"""Private, manifest-bound evidence archive for offline legacy migration.

This module is deliberately outside every agentacct runtime read/write path.
It accepts only an already verified snapshot and an explicit, existing,
owner-only archive directory.  Source bytes are copied without interpretation,
physical JSONL lines receive stable byte locators, and migration decisions are
published as immutable, hash-chained receipts.

The archive is evidence for a disposable migration candidate.  It is not a
runtime store, adapter, shadow database, or cutover mechanism.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Iterator, Literal, Mapping, Sequence

from .live_paths import LivePathSafetyError, paths_overlap, reject_live_state_overlap
from .snapshot import SnapshotSafetyError, VerifiedSnapshot


ARCHIVE_SCHEMA = "agent-chronicle.migration-evidence-archive.v1"
LINE_INDEX_SCHEMA = "agent-chronicle.migration-evidence-line.v1"
RECEIPT_SCHEMA = "migration-recovery-receipt-v1"
RECEIPT_DIGEST_DOMAIN = b"agent-chronicle-migration-recovery-receipt-v1\x00"
ARCHIVE_DIGEST_DOMAIN = b"agent-chronicle-migration-evidence-archive-v1\x00"

PAYLOAD_FILE = "payload-v1.bin"
LINE_INDEX_FILE = "line-index-v1.jsonl"
ARCHIVE_MANIFEST_FILE = "archive-manifest-v1.json"
ARCHIVE_LOCK_FILE = "archive.lock"

_IO_CHUNK_BYTES = 1024 * 1024
_MAX_METADATA_BYTES = 256 * 1024 * 1024
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,127}\Z")
_REASON_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_RECEIPT_NAME_RE = re.compile(r"receipt-([0-9]{12})-([0-9a-f]{64})\.json\Z")

ReceiptDisposition = Literal[
    "candidate_recovery",
    "namespace_only",
    "identity_conflict",
    "ambiguous",
    "no_proof",
    "unsupported_retained",
    "canonical_imported",
    "canonical_no_effect",
    "canonical_conflict_quarantined",
    "invalid_retained",
]

_DISPOSITIONS = frozenset(
    {
        "candidate_recovery",
        "namespace_only",
        "identity_conflict",
        "ambiguous",
        "no_proof",
        "unsupported_retained",
        "canonical_imported",
        "canonical_no_effect",
        "canonical_conflict_quarantined",
        "invalid_retained",
    }
)
_DONOR_REQUIRED = frozenset({"candidate_recovery", "identity_conflict", "ambiguous"})


class MigrationArchiveError(SnapshotSafetyError):
    """The archive path, static evidence, or receipt chain is unsafe."""


class ReceiptValidationError(MigrationArchiveError):
    """A receipt does not conform to the frozen v1 schema."""


class ReceiptCASMismatch(MigrationArchiveError):
    """A new receipt was based on a stale archive head."""


class ReceiptConflictError(MigrationArchiveError):
    """One design attempted two decisions for the same physical subject."""


def _require_hex64(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise ReceiptValidationError(f"{label} must be lowercase SHA-256 hex")
    return value


def _require_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ReceiptValidationError(f"{label} is not a canonical identifier")
    return value


def _require_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReceiptValidationError(f"{label} must be an integer >= {minimum}")
    return value


def _validate_unicode(value: str, *, label: str, maximum: int) -> str:
    if not value or len(value) > maximum:
        raise ReceiptValidationError(f"{label} must contain 1..{maximum} code points")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ReceiptValidationError(f"{label} may not contain lone surrogates")
    return value


def _canonical_relative_path(value: object) -> str:
    if not isinstance(value, str):
        raise ReceiptValidationError("relative_path must be a string")
    _validate_unicode(value, label="relative_path", maximum=4096)
    if "\\" in value or any(ord(character) < 32 for character in value):
        raise ReceiptValidationError("relative_path contains forbidden characters")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ReceiptValidationError("relative_path must be canonical and root-relative")
    if pure.as_posix() != value:
        raise ReceiptValidationError("relative_path must be canonical")
    return value


@dataclass(frozen=True, slots=True)
class PhysicalLineLocator:
    """One distinct physical source line, including its exact raw bytes."""

    snapshot_manifest_digest: str
    relative_path: str
    line_number: int
    byte_offset: int
    byte_length: int
    raw_sha256: str

    def __post_init__(self) -> None:
        _require_hex64(self.snapshot_manifest_digest, label="snapshot_manifest_digest")
        _canonical_relative_path(self.relative_path)
        _require_int(self.line_number, label="line_number", minimum=1)
        _require_int(self.byte_offset, label="byte_offset")
        _require_int(self.byte_length, label="byte_length", minimum=1)
        _require_hex64(self.raw_sha256, label="raw_sha256")

    def sort_key(self) -> tuple[bytes, bytes, int, int, bytes]:
        return (
            self.snapshot_manifest_digest.encode("ascii"),
            self.relative_path.encode("utf-8"),
            self.byte_offset,
            self.byte_length,
            self.raw_sha256.encode("ascii"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "byte_length": self.byte_length,
            "byte_offset": self.byte_offset,
            "line_number": self.line_number,
            "raw_sha256": self.raw_sha256,
            "relative_path": self.relative_path,
            "snapshot_manifest_digest": self.snapshot_manifest_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "PhysicalLineLocator":
        _require_exact_keys(
            value,
            {
                "snapshot_manifest_digest",
                "relative_path",
                "line_number",
                "byte_offset",
                "byte_length",
                "raw_sha256",
            },
            label="locator",
        )
        return cls(
            snapshot_manifest_digest=value["snapshot_manifest_digest"],  # type: ignore[arg-type]
            relative_path=value["relative_path"],  # type: ignore[arg-type]
            line_number=value["line_number"],  # type: ignore[arg-type]
            byte_offset=value["byte_offset"],  # type: ignore[arg-type]
            byte_length=value["byte_length"],  # type: ignore[arg-type]
            raw_sha256=value["raw_sha256"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PhysicalInventoryFile:
    relative_path: str
    size_bytes: int
    sha256: str
    line_count: int

    def __post_init__(self) -> None:
        _canonical_relative_path(self.relative_path)
        _require_int(self.size_bytes, label="size_bytes")
        _require_hex64(self.sha256, label="sha256")
        _require_int(self.line_count, label="line_count")


@dataclass(frozen=True, slots=True)
class PhysicalInventory:
    snapshot_manifest_digest: str
    files: tuple[PhysicalInventoryFile, ...]
    lines: tuple[PhysicalLineLocator, ...]

    def __post_init__(self) -> None:
        _require_hex64(self.snapshot_manifest_digest, label="snapshot_manifest_digest")
        ordered_files = tuple(sorted(self.files, key=lambda item: item.relative_path.encode("utf-8")))
        if ordered_files != self.files or len({item.relative_path for item in self.files}) != len(self.files):
            raise MigrationArchiveError("inventory files must be unique and UTF-8-byte ordered")
        ordered_lines = tuple(sorted(self.lines, key=PhysicalLineLocator.sort_key))
        if ordered_lines != self.lines or len(set(self.lines)) != len(self.lines):
            raise MigrationArchiveError("inventory lines must be unique and canonically ordered")
        lines_by_file: dict[str, list[PhysicalLineLocator]] = {
            item.relative_path: [] for item in self.files
        }
        for locator in self.lines:
            if locator.snapshot_manifest_digest != self.snapshot_manifest_digest:
                raise MigrationArchiveError("line locator names another snapshot manifest")
            if locator.relative_path not in lines_by_file:
                raise MigrationArchiveError("line locator names an undeclared inventory file")
            lines_by_file[locator.relative_path].append(locator)
        for item in self.files:
            expected_offset = 0
            lines = lines_by_file[item.relative_path]
            for expected_line, locator in enumerate(lines, start=1):
                if locator.line_number != expected_line or locator.byte_offset != expected_offset:
                    raise MigrationArchiveError("physical line ranges contain a gap, overlap, or line-number jump")
                expected_offset += locator.byte_length
            if expected_offset != item.size_bytes or len(lines) != item.line_count:
                raise MigrationArchiveError("physical line ranges do not conserve exact file bytes")

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    @property
    def total_lines(self) -> int:
        return len(self.lines)


@dataclass(frozen=True, slots=True)
class RecoveryIdentity:
    client: str
    source_namespace_fingerprint: str
    client_session_id: str
    provenance: str = "high/log-evidenced"
    fact_transport: str = "legacy_client_log_recovery_v1"
    link_method: str = "client_log_evidenced_recovery"
    link_confidence: str = "high"
    rule_version: str = "legacy-client-log-recovery-v1"

    def __post_init__(self) -> None:
        _validate_unicode(self.client, label="recovery.client", maximum=64)
        _validate_unicode(
            self.source_namespace_fingerprint,
            label="recovery.source_namespace_fingerprint",
            maximum=512,
        )
        _validate_unicode(self.client_session_id, label="recovery.client_session_id", maximum=512)
        fixed = {
            "provenance": (self.provenance, "high/log-evidenced"),
            "fact_transport": (self.fact_transport, "legacy_client_log_recovery_v1"),
            "link_method": (self.link_method, "client_log_evidenced_recovery"),
            "link_confidence": (self.link_confidence, "high"),
            "rule_version": (self.rule_version, "legacy-client-log-recovery-v1"),
        }
        for label, (observed, expected) in fixed.items():
            if observed != expected:
                raise ReceiptValidationError(f"recovery.{label} must equal {expected!r}")

    def to_dict(self) -> dict[str, object]:
        return {
            "client": self.client,
            "client_session_id": self.client_session_id,
            "fact_transport": self.fact_transport,
            "link_confidence": self.link_confidence,
            "link_method": self.link_method,
            "provenance": self.provenance,
            "rule_version": self.rule_version,
            "source_namespace_fingerprint": self.source_namespace_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RecoveryIdentity":
        _require_exact_keys(
            value,
            {
                "client",
                "client_session_id",
                "fact_transport",
                "link_confidence",
                "link_method",
                "provenance",
                "rule_version",
                "source_namespace_fingerprint",
            },
            label="recovery",
        )
        if not all(isinstance(item, str) for item in value.values()):
            raise ReceiptValidationError("recovery fields must be strings")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReceiptDraft:
    design_id: str
    subject: PhysicalLineLocator
    disposition: ReceiptDisposition
    classifier: str
    classifier_version: str
    rule_version: str
    rules_digest: str
    reason_codes: tuple[str, ...]
    donors: tuple[PhysicalLineLocator, ...] = ()
    recovery: RecoveryIdentity | None = None
    supersedes_receipt_digest: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.design_id, label="design_id")
        if self.disposition not in _DISPOSITIONS:
            raise ReceiptValidationError("receipt disposition is not allowed")
        _require_identifier(self.classifier, label="classifier")
        _require_identifier(self.classifier_version, label="classifier_version")
        _require_identifier(self.rule_version, label="rule_version")
        _require_hex64(self.rules_digest, label="rules_digest")
        if tuple(sorted(self.donors, key=PhysicalLineLocator.sort_key)) != self.donors:
            raise ReceiptValidationError("receipt donors must use canonical locator order")
        if len(set(self.donors)) != len(self.donors):
            raise ReceiptValidationError("receipt donors must be deduplicated")
        if self.subject in self.donors:
            raise ReceiptValidationError("a receipt subject may not donate to itself")
        if tuple(sorted(self.reason_codes)) != self.reason_codes or len(set(self.reason_codes)) != len(self.reason_codes):
            raise ReceiptValidationError("reason_codes must be sorted and unique")
        if not self.reason_codes or any(_REASON_RE.fullmatch(item) is None for item in self.reason_codes):
            raise ReceiptValidationError("reason_codes must contain canonical identifiers")
        if self.disposition == "candidate_recovery":
            if len(self.donors) != 1 or self.recovery is None:
                raise ReceiptValidationError("candidate_recovery requires one donor and recovery identity")
        elif self.disposition in {"identity_conflict", "ambiguous"}:
            if not self.donors or self.recovery is not None:
                raise ReceiptValidationError(f"{self.disposition} requires donors and forbids recovery identity")
        elif self.donors or self.recovery is not None:
            raise ReceiptValidationError(f"{self.disposition} forbids donors and recovery identity")
        if self.supersedes_receipt_digest is not None:
            _require_hex64(
                self.supersedes_receipt_digest,
                label="supersedes_receipt_digest",
            )
            raise ReceiptValidationError(
                "migration-recovery-receipt-v1 requires supersedes_receipt_digest to be null"
            )

    def decision_dict(self) -> dict[str, object]:
        return {
            "classification": {
                "classifier": self.classifier,
                "classifier_version": self.classifier_version,
                "reason_codes": list(self.reason_codes),
                "rule_version": self.rule_version,
                "rules_digest": self.rules_digest,
            },
            "design_id": self.design_id,
            "disposition": self.disposition,
            "donors": [item.to_dict() for item in self.donors],
            "recovery": self.recovery.to_dict() if self.recovery is not None else None,
            "subject": self.subject.to_dict(),
            "supersedes_receipt_digest": self.supersedes_receipt_digest,
        }


@dataclass(frozen=True, slots=True)
class MigrationRecoveryReceipt:
    archive_manifest_digest: str
    sequence: int
    previous_receipt_digest: str | None
    draft: ReceiptDraft

    def __post_init__(self) -> None:
        _require_hex64(self.archive_manifest_digest, label="archive_manifest_digest")
        _require_int(self.sequence, label="sequence", minimum=1)
        if self.sequence == 1:
            if self.previous_receipt_digest is not None:
                raise ReceiptValidationError("the first receipt must have a null previous digest")
        else:
            _require_hex64(self.previous_receipt_digest, label="previous_receipt_digest")

    def to_dict(self) -> dict[str, object]:
        value = self.draft.decision_dict()
        value.update(
            {
                "archive_manifest_digest": self.archive_manifest_digest,
                "previous_receipt_digest": self.previous_receipt_digest,
                "schema": RECEIPT_SCHEMA,
                "sequence": self.sequence,
            }
        )
        return value


@dataclass(frozen=True, slots=True)
class ArchiveHead:
    sequence: int
    digest: str | None

    def __post_init__(self) -> None:
        _require_int(self.sequence, label="head.sequence")
        if self.sequence == 0:
            if self.digest is not None:
                raise ReceiptValidationError("an empty head must have a null digest")
        else:
            _require_hex64(self.digest, label="head.digest")


EMPTY_ARCHIVE_HEAD = ArchiveHead(0, None)


@dataclass(frozen=True, slots=True)
class ReceiptPublishResult:
    status: Literal["published", "noop"]
    receipt_digest: str
    receipt_sequence: int
    head: ArchiveHead


@dataclass(frozen=True, slots=True)
class VerifiedRecoveryDecision:
    subject: PhysicalLineLocator
    donor: PhysicalLineLocator
    recovery: RecoveryIdentity
    receipt_digest: str


def _validate_json_value(value: object, *, path: str = "$" ) -> None:
    if value is None or isinstance(value, str):
        if isinstance(value, str) and any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ReceiptValidationError(f"{path} contains a lone surrogate")
        return
    if isinstance(value, bool) or isinstance(value, float):
        raise ReceiptValidationError(f"{path} may contain integer numbers only")
    if isinstance(value, int):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReceiptValidationError(f"{path} object keys must be strings")
            _validate_json_value(key, path=f"{path}.<key>")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ReceiptValidationError(f"{path} contains an unsupported JSON value")


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    _validate_json_value(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError) as exc:
        raise ReceiptValidationError("value cannot be encoded as strict canonical UTF-8 JSON") from exc


def canonical_receipt_bytes(receipt: MigrationRecoveryReceipt) -> bytes:
    """Encode one schema-validated receipt using the frozen v1 codec."""

    if not isinstance(receipt, MigrationRecoveryReceipt):
        raise ReceiptValidationError("receipt must be a MigrationRecoveryReceipt")
    return _canonical_json_bytes(receipt.to_dict())


def receipt_digest(receipt: MigrationRecoveryReceipt) -> str:
    """Return the domain-separated lowercase SHA-256 receipt identity."""

    return hashlib.sha256(RECEIPT_DIGEST_DOMAIN + canonical_receipt_bytes(receipt)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptValidationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_noninteger_number(raw: str) -> object:
    raise ReceiptValidationError(f"non-integer JSON number is forbidden: {raw!r}")


def _decode_canonical_json(raw: bytes, *, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_noninteger_number,
            parse_constant=_reject_noninteger_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptValidationError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise ReceiptValidationError(f"{label} must be a JSON object")
    _validate_json_value(value)
    if _canonical_json_bytes(value) != raw:
        raise ReceiptValidationError(f"{label} is not canonically encoded")
    return value


def _require_exact_keys(value: Mapping[str, object], expected: set[str], *, label: str) -> None:
    observed = set(value)
    if observed != expected:
        raise ReceiptValidationError(
            f"{label} fields differ from schema (missing={sorted(expected-observed)}, unknown={sorted(observed-expected)})"
        )


def _receipt_from_bytes(raw: bytes) -> MigrationRecoveryReceipt:
    value = _decode_canonical_json(raw, label="receipt")
    _require_exact_keys(
        value,
        {
            "archive_manifest_digest",
            "classification",
            "design_id",
            "disposition",
            "donors",
            "previous_receipt_digest",
            "recovery",
            "schema",
            "sequence",
            "subject",
            "supersedes_receipt_digest",
        },
        label="receipt",
    )
    if value["schema"] != RECEIPT_SCHEMA:
        raise ReceiptValidationError("receipt schema is not supported")
    subject_value = value["subject"]
    donors_value = value["donors"]
    classification = value["classification"]
    if not isinstance(subject_value, Mapping) or not isinstance(donors_value, list) or not isinstance(classification, Mapping):
        raise ReceiptValidationError("receipt subject, donors, or classification has the wrong shape")
    _require_exact_keys(
        classification,
        {"classifier", "classifier_version", "reason_codes", "rule_version", "rules_digest"},
        label="classification",
    )
    reason_codes = classification["reason_codes"]
    if not isinstance(reason_codes, list) or not all(isinstance(item, str) for item in reason_codes):
        raise ReceiptValidationError("classification.reason_codes must be a string array")
    donors: list[PhysicalLineLocator] = []
    for item in donors_value:
        if not isinstance(item, Mapping):
            raise ReceiptValidationError("each donor must be a locator object")
        donors.append(PhysicalLineLocator.from_dict(item))
    recovery_value = value["recovery"]
    if recovery_value is None:
        recovery = None
    elif isinstance(recovery_value, Mapping):
        recovery = RecoveryIdentity.from_dict(recovery_value)
    else:
        raise ReceiptValidationError("receipt recovery must be an object or null")
    draft = ReceiptDraft(
        design_id=value["design_id"],  # type: ignore[arg-type]
        subject=PhysicalLineLocator.from_dict(subject_value),
        disposition=value["disposition"],  # type: ignore[arg-type]
        classifier=classification["classifier"],  # type: ignore[arg-type]
        classifier_version=classification["classifier_version"],  # type: ignore[arg-type]
        rule_version=classification["rule_version"],  # type: ignore[arg-type]
        rules_digest=classification["rules_digest"],  # type: ignore[arg-type]
        reason_codes=tuple(reason_codes),
        donors=tuple(donors),
        recovery=recovery,
        supersedes_receipt_digest=value["supersedes_receipt_digest"],  # type: ignore[arg-type]
    )
    receipt = MigrationRecoveryReceipt(
        archive_manifest_digest=value["archive_manifest_digest"],  # type: ignore[arg-type]
        sequence=value["sequence"],  # type: ignore[arg-type]
        previous_receipt_digest=value["previous_receipt_digest"],  # type: ignore[arg-type]
        draft=draft,
    )
    if canonical_receipt_bytes(receipt) != raw:
        raise ReceiptValidationError("receipt did not round-trip through the v1 schema")
    return receipt


def scan_snapshot_lines(snapshot: VerifiedSnapshot) -> PhysicalInventory:
    """Inventory every physical line of every manifest-declared snapshot file."""

    if not isinstance(snapshot, VerifiedSnapshot):
        raise MigrationArchiveError("source must be a VerifiedSnapshot")
    snapshot.verify_unchanged()
    files: list[PhysicalInventoryFile] = []
    lines: list[PhysicalLineLocator] = []
    for source in sorted(snapshot.files, key=lambda item: item.relative_path.encode("utf-8")):
        byte_offset = 0
        line_number = 0
        line_length = 0
        line_hash = hashlib.sha256()
        with snapshot.open_binary(source.relative_path) as handle:
            while block := handle.read(_IO_CHUNK_BYTES):
                cursor = 0
                while cursor < len(block):
                    newline = block.find(b"\n", cursor)
                    end = len(block) if newline < 0 else newline + 1
                    part = block[cursor:end]
                    line_hash.update(part)
                    line_length += len(part)
                    cursor = end
                    if newline >= 0:
                        line_number += 1
                        lines.append(
                            PhysicalLineLocator(
                                snapshot_manifest_digest=snapshot.manifest_digest,
                                relative_path=source.relative_path,
                                line_number=line_number,
                                byte_offset=byte_offset,
                                byte_length=line_length,
                                raw_sha256=line_hash.hexdigest(),
                            )
                        )
                        byte_offset += line_length
                        line_length = 0
                        line_hash = hashlib.sha256()
        if line_length:
            line_number += 1
            lines.append(
                PhysicalLineLocator(
                    snapshot_manifest_digest=snapshot.manifest_digest,
                    relative_path=source.relative_path,
                    line_number=line_number,
                    byte_offset=byte_offset,
                    byte_length=line_length,
                    raw_sha256=line_hash.hexdigest(),
                )
            )
            byte_offset += line_length
        if byte_offset != source.size_bytes:
            raise MigrationArchiveError("physical line scan did not conserve exact source bytes")
        files.append(
            PhysicalInventoryFile(
                relative_path=source.relative_path,
                size_bytes=source.size_bytes,
                sha256=source.sha256,
                line_count=line_number,
            )
        )
    inventory = PhysicalInventory(
        snapshot_manifest_digest=snapshot.manifest_digest,
        files=tuple(files),
        lines=tuple(sorted(lines, key=PhysicalLineLocator.sort_key)),
    )
    snapshot.verify_unchanged()
    return inventory


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_flags(flags: int) -> int:
    return flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise MigrationArchiveError("archive directory must already exist") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise MigrationArchiveError("archive directory may not contain symlink components")


class _AnchoredDirectory:
    def __init__(self, path: Path, descriptor: int, identity: tuple[int, int]) -> None:
        self.path = path
        self.descriptor = descriptor
        self.identity = identity
        self.closed = False

    @classmethod
    def open(cls, path_value: Path | str, *, snapshot: VerifiedSnapshot) -> "_AnchoredDirectory":
        path = Path(path_value)
        if not path.is_absolute():
            raise MigrationArchiveError("archive directory must be an explicit absolute path")
        try:
            reject_live_state_overlap(path, label="migration archive")
        except LivePathSafetyError as exc:
            raise MigrationArchiveError(str(exc)) from exc
        _assert_no_symlink_components(path)
        try:
            resolved = path.resolve(strict=True)
            observed = path.stat(follow_symlinks=False)
        except (OSError, RuntimeError) as exc:
            raise MigrationArchiveError("archive directory cannot be resolved safely") from exc
        if resolved != path:
            raise MigrationArchiveError("archive directory path changed during resolution")
        if paths_overlap(path, snapshot.root) or paths_overlap(path, snapshot.manifest.path):
            raise MigrationArchiveError("archive directory must be disjoint from the verified snapshot and manifest")
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) & 0o077
        ):
            raise MigrationArchiveError("archive directory must be owner-only and owned by the current user")
        try:
            descriptor = os.open(path, _directory_flags())
        except OSError as exc:
            raise MigrationArchiveError("archive directory cannot be opened safely") from exc
        anchored = cls(path, descriptor, _identity(observed))
        try:
            anchored.prove_unchanged()
        except BaseException:
            anchored.close()
            raise
        return anchored

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self.descriptor)

    def prove_unchanged(self) -> None:
        if self.closed:
            raise MigrationArchiveError("archive directory is closed")
        _assert_no_symlink_components(self.path)
        try:
            path_stat = self.path.stat(follow_symlinks=False)
            descriptor_stat = os.fstat(self.descriptor)
        except OSError as exc:
            raise MigrationArchiveError("archive directory identity changed") from exc
        for observed in (path_stat, descriptor_stat):
            if (
                not stat.S_ISDIR(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or stat.S_IMODE(observed.st_mode) & 0o077
                or _identity(observed) != self.identity
            ):
                raise MigrationArchiveError("archive directory identity or permissions changed")

    def names(self) -> tuple[str, ...]:
        self.prove_unchanged()
        with os.scandir(self.descriptor) as entries:
            result = tuple(sorted(entry.name for entry in entries))
        self.prove_unchanged()
        return result

    def open_file(self, name: str, flags: int, mode: int = 0o600) -> int:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise MigrationArchiveError("archive file must be one direct child name")
        self.prove_unchanged()
        try:
            descriptor = os.open(name, _file_flags(flags), mode, dir_fd=self.descriptor)
        except OSError:
            self.prove_unchanged()
            raise
        self.prove_unchanged()
        return descriptor


def _validate_private_file(observed: os.stat_result, *, label: str, allow_staged_link: bool = False) -> None:
    maximum_links = 2 if allow_staged_link else 1
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
        or observed.st_nlink < 1
        or observed.st_nlink > maximum_links
    ):
        raise MigrationArchiveError(f"{label} is not a private, non-aliased regular file")


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise MigrationArchiveError("archive write made no progress")
        offset += written


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while block := os.read(descriptor, _IO_CHUNK_BYTES):
        digest.update(block)
        size += len(block)
    return size, digest.hexdigest()


def _read_descriptor(descriptor: int, *, maximum: int, label: str) -> bytes:
    size = os.fstat(descriptor).st_size
    if size > maximum:
        raise MigrationArchiveError(f"{label} exceeds the metadata size limit")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        block = os.read(descriptor, min(_IO_CHUNK_BYTES, remaining))
        if not block:
            raise MigrationArchiveError(f"{label} ended before its stat size")
        chunks.append(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise MigrationArchiveError(f"{label} changed while it was read")
    return b"".join(chunks)


@dataclass(slots=True)
class _StagedFile:
    name: str
    descriptor: int
    identity: tuple[int, int]
    size: int
    sha256: str


def _stage_bytes(directory: _AnchoredDirectory, final_name: str, value: bytes) -> _StagedFile:
    name = f".{final_name}.staged-{os.getpid()}-{secrets.token_hex(8)}"
    descriptor = directory.open_file(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, value)
        os.fsync(descriptor)
        observed = os.fstat(descriptor)
        _validate_private_file(observed, label="staged archive file")
        size, digest = _hash_descriptor(descriptor)
        if size != len(value) or digest != hashlib.sha256(value).hexdigest():
            raise MigrationArchiveError("staged archive file failed content verification")
        return _StagedFile(name, descriptor, _identity(observed), size, digest)
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(name, dir_fd=directory.descriptor)
        except OSError:
            pass
        raise


def _publish_staged(directory: _AnchoredDirectory, staged: _StagedFile, final_name: str) -> None:
    linked = False
    try:
        try:
            os.link(
                staged.name,
                final_name,
                src_dir_fd=directory.descriptor,
                dst_dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise MigrationArchiveError(f"archive final already exists: {final_name}") from exc
        linked = True
        os.fsync(directory.descriptor)
        final_stat = os.stat(final_name, dir_fd=directory.descriptor, follow_symlinks=False)
        if _identity(final_stat) != staged.identity or _identity(os.fstat(staged.descriptor)) != staged.identity:
            raise MigrationArchiveError("archive file identity changed during no-replace publication")
        os.unlink(staged.name, dir_fd=directory.descriptor)
        os.fsync(directory.descriptor)
        final_stat = os.stat(final_name, dir_fd=directory.descriptor, follow_symlinks=False)
        _validate_private_file(final_stat, label="published archive file")
        if _identity(final_stat) != staged.identity:
            raise MigrationArchiveError("published archive file identity changed")
        size, digest = _hash_descriptor(staged.descriptor)
        if size != staged.size or digest != staged.sha256:
            raise MigrationArchiveError("published archive file content changed")
        directory.prove_unchanged()
    except BaseException:
        # A linked final is never overwritten or silently removed.  If a
        # process dies after the durable link, the complete final is valid
        # evidence; chain validation decides whether it is authoritative.
        if not linked:
            try:
                os.unlink(staged.name, dir_fd=directory.descriptor)
            except OSError:
                pass
        raise
    finally:
        os.close(staged.descriptor)


@contextmanager
def _exclusive_lock(directory: _AnchoredDirectory) -> Iterator[int]:
    try:
        descriptor = directory.open_file(ARCHIVE_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise MigrationArchiveError("archive lock cannot be opened safely") from exc
    try:
        observed = os.fstat(descriptor)
        _validate_private_file(observed, label="archive lock")
        lock_identity = _identity(observed)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        directory.prove_unchanged()
        path_stat = os.stat(ARCHIVE_LOCK_FILE, dir_fd=directory.descriptor, follow_symlinks=False)
        if _identity(path_stat) != lock_identity or _identity(os.fstat(descriptor)) != lock_identity:
            raise MigrationArchiveError("archive lock path identity changed")
        yield descriptor
        directory.prove_unchanged()
        path_stat = os.stat(ARCHIVE_LOCK_FILE, dir_fd=directory.descriptor, follow_symlinks=False)
        if _identity(path_stat) != lock_identity:
            raise MigrationArchiveError("archive lock path identity changed")
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _copy_payload(snapshot: VerifiedSnapshot, directory: _AnchoredDirectory) -> tuple[_StagedFile, list[dict[str, object]]]:
    name = f".{PAYLOAD_FILE}.staged-{os.getpid()}-{secrets.token_hex(8)}"
    descriptor = directory.open_file(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    payload_offset = 0
    files: list[dict[str, object]] = []
    try:
        os.fchmod(descriptor, 0o600)
        for source in sorted(snapshot.files, key=lambda item: item.relative_path.encode("utf-8")):
            start = payload_offset
            with snapshot.open_binary(source.relative_path) as handle:
                while block := handle.read(_IO_CHUNK_BYTES):
                    _write_all(descriptor, block)
                    digest.update(block)
                    payload_offset += len(block)
            if payload_offset - start != source.size_bytes:
                raise MigrationArchiveError("payload copy size differs from verified snapshot")
            files.append(
                {
                    "line_count": 0,
                    "payload_offset": start,
                    "relative_path": source.relative_path,
                    "sha256": source.sha256,
                    "size_bytes": source.size_bytes,
                }
            )
        os.fsync(descriptor)
        observed = os.fstat(descriptor)
        _validate_private_file(observed, label="staged payload")
        if observed.st_size != payload_offset:
            raise MigrationArchiveError("staged payload stat size differs from copied bytes")
        return _StagedFile(name, descriptor, _identity(observed), payload_offset, digest.hexdigest()), files
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(name, dir_fd=directory.descriptor)
        except OSError:
            pass
        raise


def build_migration_archive(
    *,
    snapshot: VerifiedSnapshot,
    archive_root: Path | str,
    inventory: PhysicalInventory | None = None,
) -> "VerifiedMigrationArchive":
    """Build one immutable static archive in an existing empty private directory."""

    if not isinstance(snapshot, VerifiedSnapshot):
        raise MigrationArchiveError("source must be a VerifiedSnapshot")
    if inventory is None:
        inventory = scan_snapshot_lines(snapshot)
    elif inventory != scan_snapshot_lines(snapshot):
        raise MigrationArchiveError("supplied inventory does not match the verified snapshot")
    directory = _AnchoredDirectory.open(archive_root, snapshot=snapshot)
    try:
        with _exclusive_lock(directory):
            if set(directory.names()) != {ARCHIVE_LOCK_FILE}:
                raise MigrationArchiveError("new migration archive directory must be empty")
            payload, file_values = _copy_payload(snapshot, directory)
            line_counts = {item.relative_path: item.line_count for item in inventory.files}
            for item in file_values:
                item["line_count"] = line_counts[str(item["relative_path"])]
            file_offsets = {str(item["relative_path"]): int(item["payload_offset"]) for item in file_values}
            index_bytes = b"".join(
                _canonical_json_bytes(
                    {
                        "locator": locator.to_dict(),
                        "payload_offset": file_offsets[locator.relative_path] + locator.byte_offset,
                        "schema": LINE_INDEX_SCHEMA,
                    }
                )
                + b"\n"
                for locator in inventory.lines
            )
            index = _stage_bytes(directory, LINE_INDEX_FILE, index_bytes)
            manifest_value: dict[str, object] = {
                "files": file_values,
                "line_index": {
                    "file": LINE_INDEX_FILE,
                    "line_count": inventory.total_lines,
                    "sha256": index.sha256,
                    "size_bytes": index.size,
                },
                "payload": {
                    "file": PAYLOAD_FILE,
                    "sha256": payload.sha256,
                    "size_bytes": payload.size,
                },
                "schema": ARCHIVE_SCHEMA,
                "snapshot": {
                    "kind": snapshot.kind,
                    "manifest_digest": snapshot.manifest_digest,
                },
                "totals": {
                    "file_count": len(inventory.files),
                    "physical_line_count": inventory.total_lines,
                    "source_bytes": inventory.total_bytes,
                },
            }
            manifest_bytes = _canonical_json_bytes(manifest_value)
            manifest = _stage_bytes(directory, ARCHIVE_MANIFEST_FILE, manifest_bytes)
            snapshot.verify_unchanged()
            _publish_staged(directory, payload, PAYLOAD_FILE)
            _publish_staged(directory, index, LINE_INDEX_FILE)
            # Manifest publication is the static archive commit marker and is
            # deliberately last.
            _publish_staged(directory, manifest, ARCHIVE_MANIFEST_FILE)
            os.fsync(directory.descriptor)
            snapshot.verify_unchanged()
    finally:
        directory.close()
    return VerifiedMigrationArchive.open(snapshot=snapshot, archive_root=archive_root)


def _open_private_child(directory: _AnchoredDirectory, name: str, *, maximum: int | None = None) -> tuple[int, os.stat_result]:
    try:
        descriptor = directory.open_file(name, os.O_RDONLY)
    except OSError as exc:
        raise MigrationArchiveError(f"archive file is missing or unsafe: {name}") from exc
    try:
        observed = os.fstat(descriptor)
        _validate_private_file(observed, label=name, allow_staged_link=True)
        path_stat = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        if _identity(path_stat) != _identity(observed):
            raise MigrationArchiveError(f"archive file path identity changed: {name}")
        if observed.st_nlink == 2:
            aliases: list[str] = []
            expected_prefix = f".{name}.staged-"
            for candidate in directory.names():
                if not candidate.startswith(expected_prefix):
                    continue
                candidate_stat = os.stat(
                    candidate,
                    dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
                if _identity(candidate_stat) == _identity(observed):
                    _validate_private_file(
                        candidate_stat,
                        label="interrupted staged publication alias",
                        allow_staged_link=True,
                    )
                    aliases.append(candidate)
            if len(aliases) != 1:
                raise MigrationArchiveError(
                    f"archive file has an unexplained hard-link alias: {name}"
                )
        if maximum is not None and observed.st_size > maximum:
            raise MigrationArchiveError(f"archive file exceeds safety limit: {name}")
        return descriptor, observed
    except BaseException:
        os.close(descriptor)
        raise


def _parse_archive_manifest(raw: bytes) -> Mapping[str, object]:
    value = _decode_canonical_json(raw, label="archive manifest")
    _require_exact_keys(value, {"files", "line_index", "payload", "schema", "snapshot", "totals"}, label="archive manifest")
    if value["schema"] != ARCHIVE_SCHEMA:
        raise MigrationArchiveError("archive manifest schema is not supported")
    return value


class VerifiedMigrationArchive:
    """Descriptor-anchored static evidence plus a validated receipt chain."""

    def __init__(
        self,
        *,
        snapshot: VerifiedSnapshot,
        directory: _AnchoredDirectory,
        manifest_value: Mapping[str, object],
        manifest_digest: str,
        inventory: PhysicalInventory,
        payload_offsets: Mapping[str, int],
    ) -> None:
        self.snapshot = snapshot
        self._directory = directory
        self.manifest = MappingProxyType(dict(manifest_value))
        self.manifest_digest = manifest_digest
        self.inventory = inventory
        self._payload_offsets = MappingProxyType(dict(payload_offsets))
        self._locators = frozenset(inventory.lines)
        self._closed = False

    @classmethod
    def open(
        cls,
        *,
        snapshot: VerifiedSnapshot,
        archive_root: Path | str,
    ) -> "VerifiedMigrationArchive":
        if not isinstance(snapshot, VerifiedSnapshot):
            raise MigrationArchiveError("source must be a VerifiedSnapshot")
        directory = _AnchoredDirectory.open(archive_root, snapshot=snapshot)
        try:
            manifest_fd, _ = _open_private_child(directory, ARCHIVE_MANIFEST_FILE, maximum=_MAX_METADATA_BYTES)
            try:
                manifest_raw = _read_descriptor(manifest_fd, maximum=_MAX_METADATA_BYTES, label="archive manifest")
            finally:
                os.close(manifest_fd)
            manifest = _parse_archive_manifest(manifest_raw)
            archive_digest = hashlib.sha256(ARCHIVE_DIGEST_DOMAIN + manifest_raw).hexdigest()
            snapshot_value = manifest["snapshot"]
            files_value = manifest["files"]
            payload_value = manifest["payload"]
            index_value = manifest["line_index"]
            totals_value = manifest["totals"]
            if not all(isinstance(item, Mapping) for item in (snapshot_value, payload_value, index_value, totals_value)) or not isinstance(files_value, list):
                raise MigrationArchiveError("archive manifest object shapes are invalid")
            _require_exact_keys(snapshot_value, {"kind", "manifest_digest"}, label="archive snapshot")
            _require_exact_keys(payload_value, {"file", "sha256", "size_bytes"}, label="archive payload")
            _require_exact_keys(index_value, {"file", "line_count", "sha256", "size_bytes"}, label="archive line index")
            _require_exact_keys(totals_value, {"file_count", "physical_line_count", "source_bytes"}, label="archive totals")
            if snapshot_value["manifest_digest"] != snapshot.manifest_digest or snapshot_value["kind"] != snapshot.kind:
                raise MigrationArchiveError("archive is bound to another verified snapshot")
            if payload_value["file"] != PAYLOAD_FILE or index_value["file"] != LINE_INDEX_FILE:
                raise MigrationArchiveError("archive static filenames are not canonical")
            payload_fd, payload_stat = _open_private_child(directory, PAYLOAD_FILE)
            try:
                payload_size, payload_digest = _hash_descriptor(payload_fd)
            finally:
                os.close(payload_fd)
            if payload_size != payload_value["size_bytes"] or payload_digest != payload_value["sha256"] or payload_stat.st_size != payload_size:
                raise MigrationArchiveError("archive payload size or digest mismatch")
            index_fd, _ = _open_private_child(directory, LINE_INDEX_FILE, maximum=_MAX_METADATA_BYTES)
            try:
                index_raw = _read_descriptor(index_fd, maximum=_MAX_METADATA_BYTES, label="line index")
            finally:
                os.close(index_fd)
            if len(index_raw) != index_value["size_bytes"] or hashlib.sha256(index_raw).hexdigest() != index_value["sha256"]:
                raise MigrationArchiveError("archive line index size or digest mismatch")
            file_inventory: list[PhysicalInventoryFile] = []
            payload_offsets: dict[str, int] = {}
            expected_payload_offset = 0
            for raw_file in files_value:
                if not isinstance(raw_file, Mapping):
                    raise MigrationArchiveError("archive files entries must be objects")
                _require_exact_keys(raw_file, {"line_count", "payload_offset", "relative_path", "sha256", "size_bytes"}, label="archive file")
                item = PhysicalInventoryFile(
                    relative_path=raw_file["relative_path"],  # type: ignore[arg-type]
                    size_bytes=raw_file["size_bytes"],  # type: ignore[arg-type]
                    sha256=raw_file["sha256"],  # type: ignore[arg-type]
                    line_count=raw_file["line_count"],  # type: ignore[arg-type]
                )
                offset = _require_int(raw_file["payload_offset"], label="payload_offset")
                if offset != expected_payload_offset:
                    raise MigrationArchiveError("archive payload file ranges contain a gap or overlap")
                expected_payload_offset += item.size_bytes
                payload_offsets[item.relative_path] = offset
                file_inventory.append(item)
            if expected_payload_offset != payload_size:
                raise MigrationArchiveError("archive payload ranges do not conserve payload bytes")
            # The archive manifest is private evidence, not an external trust
            # root.  Prove each packed segment against the already trusted
            # snapshot-manifest digest instead of trusting a self-consistent
            # rewritten payload + archive manifest.
            payload_fd, _ = _open_private_child(directory, PAYLOAD_FILE)
            try:
                for item in file_inventory:
                    os.lseek(payload_fd, payload_offsets[item.relative_path], os.SEEK_SET)
                    digest = hashlib.sha256()
                    remaining = item.size_bytes
                    while remaining:
                        block = os.read(payload_fd, min(_IO_CHUNK_BYTES, remaining))
                        if not block:
                            raise MigrationArchiveError(
                                "archive payload ended inside a source-file segment"
                            )
                        digest.update(block)
                        remaining -= len(block)
                    if digest.hexdigest() != item.sha256:
                        raise MigrationArchiveError(
                            "archive payload segment differs from verified snapshot manifest"
                        )
            finally:
                os.close(payload_fd)
            locators: list[PhysicalLineLocator] = []
            if index_raw:
                for raw_line in index_raw.splitlines(keepends=True):
                    if not raw_line.endswith(b"\n"):
                        raise MigrationArchiveError("line index must terminate every entry with LF")
                    item = _decode_canonical_json(raw_line[:-1], label="line index entry")
                    _require_exact_keys(item, {"locator", "payload_offset", "schema"}, label="line index entry")
                    if item["schema"] != LINE_INDEX_SCHEMA or not isinstance(item["locator"], Mapping):
                        raise MigrationArchiveError("line index entry schema or locator is invalid")
                    locator = PhysicalLineLocator.from_dict(item["locator"])
                    expected = payload_offsets.get(locator.relative_path)
                    if expected is None or item["payload_offset"] != expected + locator.byte_offset:
                        raise MigrationArchiveError("line index payload offset is inconsistent")
                    locators.append(locator)
            inventory = PhysicalInventory(
                snapshot_manifest_digest=snapshot.manifest_digest,
                files=tuple(file_inventory),
                lines=tuple(locators),
            )
            if inventory != scan_snapshot_lines(snapshot):
                raise MigrationArchiveError("archive physical inventory differs from verified snapshot")
            if (
                totals_value["file_count"] != len(inventory.files)
                or totals_value["physical_line_count"] != inventory.total_lines
                or totals_value["source_bytes"] != inventory.total_bytes
                or index_value["line_count"] != inventory.total_lines
            ):
                raise MigrationArchiveError("archive totals do not match physical inventory")
            result = cls(
                snapshot=snapshot,
                directory=directory,
                manifest_value=manifest,
                manifest_digest=archive_digest,
                inventory=inventory,
                payload_offsets=payload_offsets,
            )
            result.verify_unchanged()
            return result
        except BaseException:
            directory.close()
            raise

    @property
    def path(self) -> Path:
        return self._directory.path

    def __enter__(self) -> "VerifiedMigrationArchive":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._directory.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise MigrationArchiveError("verified migration archive is closed")

    def _load_receipts_locked(self) -> tuple[tuple[MigrationRecoveryReceipt, str], ...]:
        self._ensure_open()
        names = self._directory.names()
        allowed_static = {ARCHIVE_LOCK_FILE, ARCHIVE_MANIFEST_FILE, LINE_INDEX_FILE, PAYLOAD_FILE}
        receipt_names: list[tuple[int, str, str]] = []
        for name in names:
            if name in allowed_static:
                continue
            if name.startswith("."):
                if ".staged-" not in name:
                    raise MigrationArchiveError(f"unexpected hidden archive file: {name}")
                staged_stat = os.stat(
                    name,
                    dir_fd=self._directory.descriptor,
                    follow_symlinks=False,
                )
                _validate_private_file(
                    staged_stat,
                    label="interrupted staged archive file",
                    allow_staged_link=True,
                )
                continue
            match = _RECEIPT_NAME_RE.fullmatch(name)
            if match is None:
                raise MigrationArchiveError(f"unexpected archive file: {name}")
            receipt_names.append((int(match.group(1)), match.group(2), name))
        receipt_names.sort()
        expected_previous: str | None = None
        seen_subjects: set[tuple[str, PhysicalLineLocator]] = set()
        result: list[tuple[MigrationRecoveryReceipt, str]] = []
        for expected_sequence, (filename_sequence, filename_digest, name) in enumerate(receipt_names, start=1):
            if filename_sequence != expected_sequence:
                raise MigrationArchiveError("receipt chain contains a sequence gap or fork")
            descriptor, _ = _open_private_child(self._directory, name, maximum=_MAX_METADATA_BYTES)
            try:
                raw = _read_descriptor(descriptor, maximum=_MAX_METADATA_BYTES, label="receipt")
            finally:
                os.close(descriptor)
            receipt = _receipt_from_bytes(raw)
            digest = receipt_digest(receipt)
            if digest != filename_digest or receipt.sequence != filename_sequence:
                raise MigrationArchiveError("receipt filename does not match receipt identity")
            if receipt.archive_manifest_digest != self.manifest_digest:
                raise MigrationArchiveError("receipt is bound to another archive manifest")
            if receipt.previous_receipt_digest != expected_previous:
                raise MigrationArchiveError("receipt previous digest does not match the accepted chain")
            if receipt.draft.subject not in self._locators or any(donor not in self._locators for donor in receipt.draft.donors):
                raise MigrationArchiveError("receipt names a locator outside this archive")
            subject_key = (receipt.draft.design_id, receipt.draft.subject)
            if subject_key in seen_subjects:
                raise MigrationArchiveError("receipt chain contains duplicate design/subject decisions")
            seen_subjects.add(subject_key)
            result.append((receipt, digest))
            expected_previous = digest
        return tuple(result)

    def receipts(self) -> tuple[tuple[MigrationRecoveryReceipt, str], ...]:
        self._ensure_open()
        with _exclusive_lock(self._directory):
            return self._load_receipts_locked()

    def head(self) -> ArchiveHead:
        chain = self.receipts()
        if not chain:
            return EMPTY_ARCHIVE_HEAD
        return ArchiveHead(chain[-1][0].sequence, chain[-1][1])

    def receipt_for(
        self,
        locator: PhysicalLineLocator,
        *,
        design_id: str,
    ) -> tuple[MigrationRecoveryReceipt, str] | None:
        """Return the sealed receipt for one design/locator, if published."""

        _require_identifier(design_id, label="design_id")
        if locator not in self._locators:
            raise MigrationArchiveError("locator is not part of this verified archive")
        for receipt, digest in self.receipts():
            if receipt.draft.design_id == design_id and receipt.draft.subject == locator:
                return receipt, digest
        return None

    def publish_receipt(self, draft: ReceiptDraft, *, expected_head: ArchiveHead) -> ReceiptPublishResult:
        if not isinstance(draft, ReceiptDraft) or not isinstance(expected_head, ArchiveHead):
            raise ReceiptValidationError("publish requires a ReceiptDraft and ArchiveHead")
        if draft.subject not in self._locators or any(donor not in self._locators for donor in draft.donors):
            raise ReceiptValidationError("draft names a locator outside this archive")
        with _exclusive_lock(self._directory):
            chain = self._load_receipts_locked()
            current = EMPTY_ARCHIVE_HEAD if not chain else ArchiveHead(chain[-1][0].sequence, chain[-1][1])
            for existing, digest in chain:
                if existing.draft.design_id == draft.design_id and existing.draft.subject == draft.subject:
                    if existing.draft.decision_dict() == draft.decision_dict():
                        return ReceiptPublishResult("noop", digest, existing.sequence, current)
                    raise ReceiptConflictError("same design and subject already has a different receipt")
            if expected_head != current:
                raise ReceiptCASMismatch("receipt expected head is stale")
            receipt = MigrationRecoveryReceipt(
                archive_manifest_digest=self.manifest_digest,
                sequence=current.sequence + 1,
                previous_receipt_digest=current.digest,
                draft=draft,
            )
            encoded = canonical_receipt_bytes(receipt)
            digest = receipt_digest(receipt)
            final_name = f"receipt-{receipt.sequence:012d}-{digest}.json"
            staged = _stage_bytes(self._directory, final_name, encoded)
            _publish_staged(self._directory, staged, final_name)
            verified_chain = self._load_receipts_locked()
            if len(verified_chain) != len(chain) + 1 or verified_chain[-1] != (receipt, digest):
                raise MigrationArchiveError("published receipt failed post-publication chain verification")
            head = ArchiveHead(receipt.sequence, digest)
            return ReceiptPublishResult("published", digest, receipt.sequence, head)

    def publish_receipts(self, drafts: Sequence[ReceiptDraft]) -> ArchiveHead:
        """Publish a deterministic batch with one initial and one final scan.

        The real legacy corpus has thousands of physical lines.  Re-scanning
        the complete immutable chain before and after every receipt would be
        quadratic.  Keep the exclusive lock for the batch, derive the head and
        subject index once, verify each just-published file directly, then do
        one complete final-chain verification.
        """

        supplied = tuple(drafts)
        normalized = tuple(
            sorted(
                supplied,
                key=lambda item: (
                    item.design_id.encode("utf-8") if isinstance(item, ReceiptDraft) else b"",
                    item.subject.sort_key() if isinstance(item, ReceiptDraft) else (b"", b"", 0, 0, b""),
                ),
            )
        )
        if any(not isinstance(item, ReceiptDraft) for item in normalized):
            raise ReceiptValidationError("publish_receipts accepts ReceiptDraft values only")
        for draft in normalized:
            if draft.subject not in self._locators or any(
                donor not in self._locators for donor in draft.donors
            ):
                raise ReceiptValidationError("draft names a locator outside this archive")

        with _exclusive_lock(self._directory):
            chain = list(self._load_receipts_locked())
            head = (
                EMPTY_ARCHIVE_HEAD
                if not chain
                else ArchiveHead(chain[-1][0].sequence, chain[-1][1])
            )
            by_subject: dict[
                tuple[str, PhysicalLineLocator],
                tuple[ReceiptDraft, str, int],
            ] = {
                (receipt.draft.design_id, receipt.draft.subject): (
                    receipt.draft,
                    digest,
                    receipt.sequence,
                )
                for receipt, digest in chain
            }

            # Detect every same-subject disagreement before extending the
            # accepted prefix.  Identical repetitions are harmless no-ops.
            pending: dict[tuple[str, PhysicalLineLocator], ReceiptDraft] = {}
            for draft in normalized:
                key = (draft.design_id, draft.subject)
                incumbent = by_subject.get(key)
                if incumbent is not None and incumbent[0].decision_dict() != draft.decision_dict():
                    raise ReceiptConflictError(
                        "same design and subject already has a different receipt"
                    )
                prior = pending.get(key)
                if prior is not None and prior.decision_dict() != draft.decision_dict():
                    raise ReceiptConflictError(
                        "batch contains different decisions for the same design and subject"
                    )
                pending[key] = draft

            for draft in normalized:
                key = (draft.design_id, draft.subject)
                if key in by_subject:
                    continue
                receipt = MigrationRecoveryReceipt(
                    archive_manifest_digest=self.manifest_digest,
                    sequence=head.sequence + 1,
                    previous_receipt_digest=head.digest,
                    draft=draft,
                )
                encoded = canonical_receipt_bytes(receipt)
                digest = receipt_digest(receipt)
                final_name = f"receipt-{receipt.sequence:012d}-{digest}.json"
                staged = _stage_bytes(self._directory, final_name, encoded)
                _publish_staged(self._directory, staged, final_name)

                descriptor, _ = _open_private_child(
                    self._directory,
                    final_name,
                    maximum=_MAX_METADATA_BYTES,
                )
                try:
                    published_raw = _read_descriptor(
                        descriptor,
                        maximum=_MAX_METADATA_BYTES,
                        label="published receipt",
                    )
                finally:
                    os.close(descriptor)
                if published_raw != encoded or _receipt_from_bytes(published_raw) != receipt:
                    raise MigrationArchiveError(
                        "batch receipt failed direct post-publication verification"
                    )
                chain.append((receipt, digest))
                by_subject[key] = (draft, digest, receipt.sequence)
                head = ArchiveHead(receipt.sequence, digest)

            verified = self._load_receipts_locked()
            if tuple(chain) != verified:
                raise MigrationArchiveError(
                    "receipt batch failed final complete-chain verification"
                )
            return head

    def read_raw_many(
        self,
        locators: Sequence[PhysicalLineLocator],
    ) -> Mapping[PhysicalLineLocator, bytes]:
        """Cross-check many lines while opening each source file only once.

        ``VerifiedSnapshot.open_binary`` deliberately re-hashes the complete
        manifest file when its context closes.  Grouping locators prevents a
        recovery run from re-hashing one large JSONL file once per subject.
        The archive payload is likewise opened once for the whole batch.
        """

        self._ensure_open()
        requested = tuple(locators)
        if any(not isinstance(locator, PhysicalLineLocator) for locator in requested):
            raise MigrationArchiveError("recovery reads require physical line locators")
        # Duplicate requests carry no new evidence; canonicalize them to one
        # read rather than returning an ambiguous repeated mapping.
        unique = set(requested)
        if any(locator not in self._locators for locator in unique):
            raise MigrationArchiveError("locator is not part of this verified archive")
        ordered = tuple(sorted(unique, key=PhysicalLineLocator.sort_key))
        by_file: dict[str, list[PhysicalLineLocator]] = {}
        for locator in ordered:
            by_file.setdefault(locator.relative_path, []).append(locator)

        self.snapshot.verify_unchanged()
        self._directory.prove_unchanged()
        payload_fd, _ = _open_private_child(self._directory, PAYLOAD_FILE)
        results: dict[PhysicalLineLocator, bytes] = {}
        try:
            for relative_path in sorted(by_file, key=lambda value: value.encode("utf-8")):
                with self.snapshot.open_binary(relative_path) as handle:
                    for locator in by_file[relative_path]:
                        handle.seek(locator.byte_offset)
                        source_chunks: list[bytes] = []
                        source_remaining = locator.byte_length
                        while source_remaining:
                            block = handle.read(source_remaining)
                            if not block:
                                raise MigrationArchiveError(
                                    "snapshot source ended inside a physical line"
                                )
                            source_chunks.append(block)
                            source_remaining -= len(block)
                        source_raw = b"".join(source_chunks)

                        os.lseek(
                            payload_fd,
                            self._payload_offsets[relative_path] + locator.byte_offset,
                            os.SEEK_SET,
                        )
                        archived_chunks: list[bytes] = []
                        archived_remaining = locator.byte_length
                        while archived_remaining:
                            block = os.read(payload_fd, archived_remaining)
                            if not block:
                                raise MigrationArchiveError(
                                    "archive payload ended inside a physical line"
                                )
                            archived_chunks.append(block)
                            archived_remaining -= len(block)
                        archived_raw = b"".join(archived_chunks)
                        if (
                            source_raw != archived_raw
                            or len(source_raw) != locator.byte_length
                            or hashlib.sha256(source_raw).hexdigest() != locator.raw_sha256
                        ):
                            raise MigrationArchiveError(
                                "physical line bytes no longer match locator evidence"
                            )
                        results[locator] = source_raw
        finally:
            os.close(payload_fd)
        self.snapshot.verify_unchanged()
        self._directory.prove_unchanged()
        return MappingProxyType(results)

    def read_raw(self, locator: PhysicalLineLocator) -> bytes:
        """Read one line through the same batched cross-verification path."""

        return self.read_raw_many((locator,))[locator]

    def verify_unchanged(self) -> "VerifiedMigrationArchive":
        self._ensure_open()
        self._directory.prove_unchanged()
        self.snapshot.verify_unchanged()
        # Re-opening performs full static hash, inventory, and payload checks.
        # Avoid recursion here; verify the three sealed static files directly.
        for name, expected in (
            (PAYLOAD_FILE, self.manifest["payload"]),
            (LINE_INDEX_FILE, self.manifest["line_index"]),
        ):
            assert isinstance(expected, Mapping)
            descriptor, _ = _open_private_child(self._directory, name, maximum=None if name == PAYLOAD_FILE else _MAX_METADATA_BYTES)
            try:
                size, digest = _hash_descriptor(descriptor)
            finally:
                os.close(descriptor)
            if size != expected["size_bytes"] or digest != expected["sha256"]:
                raise MigrationArchiveError(f"sealed static archive file changed: {name}")
        manifest_fd, _ = _open_private_child(self._directory, ARCHIVE_MANIFEST_FILE, maximum=_MAX_METADATA_BYTES)
        try:
            raw = _read_descriptor(manifest_fd, maximum=_MAX_METADATA_BYTES, label="archive manifest")
        finally:
            os.close(manifest_fd)
        if hashlib.sha256(ARCHIVE_DIGEST_DOMAIN + raw).hexdigest() != self.manifest_digest:
            raise MigrationArchiveError("archive manifest changed after verification")
        with _exclusive_lock(self._directory):
            self._load_receipts_locked()
        return self

    def sealed_plan(self, *, design_id: str) -> "VerifiedRecoveryPlan":
        return VerifiedRecoveryPlan.from_archive(snapshot=self.snapshot, archive=self, design_id=design_id)


_PLAN_SEAL = object()


class VerifiedRecoveryPlan:
    """Complete one-receipt-per-line plan that cannot be built from raw dicts."""

    def __init__(
        self,
        *,
        archive: VerifiedMigrationArchive,
        design_id: str,
        head: ArchiveHead,
        receipts: Mapping[PhysicalLineLocator, tuple[MigrationRecoveryReceipt, str]],
        _seal: object | None = None,
    ) -> None:
        if _seal is not _PLAN_SEAL:
            raise TypeError("VerifiedRecoveryPlan must be constructed by from_archive")
        self.archive = archive
        self.design_id = design_id
        self._head = head
        self._receipts = MappingProxyType(dict(receipts))

    @classmethod
    def from_archive(
        cls,
        *,
        snapshot: VerifiedSnapshot,
        archive: VerifiedMigrationArchive,
        design_id: str,
    ) -> "VerifiedRecoveryPlan":
        if not isinstance(snapshot, VerifiedSnapshot) or not isinstance(archive, VerifiedMigrationArchive):
            raise MigrationArchiveError("plan requires verified snapshot and archive capabilities")
        _require_identifier(design_id, label="design_id")
        if snapshot.manifest_digest != archive.snapshot.manifest_digest:
            raise MigrationArchiveError("plan snapshot differs from archive snapshot")
        archive.verify_unchanged()
        chain = archive.receipts()
        selected = {
            receipt.draft.subject: (receipt, digest)
            for receipt, digest in chain
            if receipt.draft.design_id == design_id
        }
        expected = set(archive.inventory.lines)
        if set(selected) != expected:
            raise MigrationArchiveError(
                "sealed recovery plan requires exactly one target-design receipt for every physical line"
            )
        head = EMPTY_ARCHIVE_HEAD if not chain else ArchiveHead(chain[-1][0].sequence, chain[-1][1])
        return cls(
            archive=archive,
            design_id=design_id,
            head=head,
            receipts=selected,
            _seal=_PLAN_SEAL,
        )

    def __len__(self) -> int:
        return len(self._receipts)

    def iter_lines(self) -> Iterator[tuple[PhysicalLineLocator, ReceiptDisposition]]:
        for locator in sorted(self._receipts, key=PhysicalLineLocator.sort_key):
            yield locator, self._receipts[locator][0].draft.disposition

    def recovery_for(self, locator: PhysicalLineLocator) -> VerifiedRecoveryDecision | None:
        stored = self._receipts.get(locator)
        if stored is None:
            raise MigrationArchiveError("locator is not in this sealed recovery plan")
        receipt, digest = stored
        if receipt.draft.disposition != "candidate_recovery":
            return None
        assert len(receipt.draft.donors) == 1 and receipt.draft.recovery is not None
        return VerifiedRecoveryDecision(
            subject=locator,
            donor=receipt.draft.donors[0],
            recovery=receipt.draft.recovery,
            receipt_digest=digest,
        )

    def receipt_for(self, locator: PhysicalLineLocator) -> MigrationRecoveryReceipt:
        """Return one already-validated target-design receipt by locator."""

        stored = self._receipts.get(locator)
        if stored is None:
            raise MigrationArchiveError("locator is not in this sealed recovery plan")
        return stored[0]

    def read_event(self, decision: VerifiedRecoveryDecision) -> Mapping[str, object]:
        return self.read_events((decision,))[decision.subject]

    def read_events(
        self,
        decisions: Sequence[VerifiedRecoveryDecision],
    ) -> Mapping[PhysicalLineLocator, Mapping[str, object]]:
        """Validate decisions, then decode their subjects with one read per file."""

        requested = tuple(decisions)
        by_subject: dict[PhysicalLineLocator, VerifiedRecoveryDecision] = {}
        for decision in requested:
            if not isinstance(decision, VerifiedRecoveryDecision):
                raise MigrationArchiveError("recovery reads require verified decisions")
            stored = self._receipts.get(decision.subject)
            if stored is None:
                raise MigrationArchiveError(
                    "recovery decision subject is not sealed by this plan"
                )
            receipt, digest = stored
            expected = self.recovery_for(decision.subject)
            if (
                expected != decision
                or digest != decision.receipt_digest
                or receipt.draft.disposition != "candidate_recovery"
            ):
                raise MigrationArchiveError(
                    "recovery decision does not match the sealed receipt"
                )
            incumbent = by_subject.get(decision.subject)
            if incumbent is not None and incumbent != decision:
                raise MigrationArchiveError(
                    "recovery batch contains conflicting decisions for one subject"
                )
            by_subject[decision.subject] = decision

        raw_by_subject = self.archive.read_raw_many(tuple(by_subject))
        decoded: dict[PhysicalLineLocator, Mapping[str, object]] = {}
        for subject, raw in raw_by_subject.items():
            try:
                value = json.loads(
                    raw.decode("utf-8", errors="strict"),
                    object_pairs_hook=_reject_duplicate_keys,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MigrationArchiveError(
                    "sealed recovery subject is not strict JSON"
                ) from exc
            if not isinstance(value, Mapping):
                raise MigrationArchiveError(
                    "sealed recovery subject must be a JSON object"
                )
            decoded[subject] = MappingProxyType(dict(value))
        return MappingProxyType(decoded)

    def verify_unchanged(self) -> "VerifiedRecoveryPlan":
        self.archive.verify_unchanged()
        if self.archive.head() != self._head:
            raise MigrationArchiveError("archive receipt head changed after plan sealing")
        return self


__all__ = [
    "ARCHIVE_SCHEMA",
    "ArchiveHead",
    "EMPTY_ARCHIVE_HEAD",
    "MigrationArchiveError",
    "MigrationRecoveryReceipt",
    "PhysicalInventory",
    "PhysicalInventoryFile",
    "PhysicalLineLocator",
    "ReceiptCASMismatch",
    "ReceiptConflictError",
    "ReceiptDraft",
    "ReceiptPublishResult",
    "ReceiptValidationError",
    "RecoveryIdentity",
    "VerifiedMigrationArchive",
    "VerifiedRecoveryDecision",
    "VerifiedRecoveryPlan",
    "build_migration_archive",
    "canonical_receipt_bytes",
    "receipt_digest",
    "scan_snapshot_lines",
]
