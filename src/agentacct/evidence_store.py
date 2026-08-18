"""Durable append-only spool and indexed projection for Evidence v2.

The spool is the source of truth.  A complete JSON line is flushed and fsynced
before a short SQLite ``BEGIN IMMEDIATE`` transaction projects it.  If a
process dies between those steps, opening the store replays the unprojected
receipt.  Exact duplicate submissions and source conflicts remain visible as
receipt/version rows rather than being silently discarded.

Refreshable local usage is an explicit current-state exception: it reconciles
stable semantic slots under the spool lock so unchanged observations never
become receipts, while every real revision or ambiguity remains durable in a
separate ``refreshable-usage.jsonl`` transition spool.  Older binaries know
only ``spool.jsonl`` and therefore cannot consume or advance the transition
spool cursor during a downgrade.

This module owns only ``<root>/evidence-v2/``.  It never reads or writes the
historical ``events.jsonl`` ledger.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .evidence import ClaimedLink, EvidenceEnvelope, canonical_digest, canonical_json_bytes, normalize_timestamp
from .refreshable_usage import refreshable_usage_usage_material_digest_from_material


EVIDENCE_STORE_SCHEMA_VERSION = "agent-chronicle.evidence-store.v1"
EVIDENCE_SPOOL_SCHEMA_VERSION = "agent-chronicle.evidence-spool.v1"
EVIDENCE_STORE_DIRNAME = "evidence-v2"
EVIDENCE_SPOOL_FILENAME = "spool.jsonl"
EVIDENCE_PROJECTION_FILENAME = "projection.sqlite3"
REFRESHABLE_USAGE_SPOOL_SCHEMA_VERSION = "agent-chronicle.refreshable-usage-spool.v1"
REFRESHABLE_USAGE_SPOOL_FILENAME = "refreshable-usage.jsonl"

_DISPOSITIONS = frozenset({"inserted", "duplicate", "conflict"})
_ORDER_COLUMNS = {"event_time": "e.event_timestamp", "arrival": "e.first_receipt_sequence"}


def _owner_only(path: Path, mode: int) -> None:
    """Best-effort local privacy hardening for POSIX-supported deployments."""

    try:
        path.chmod(mode)
    except OSError:
        # Filesystem ACLs or platform semantics may reject chmod. The project
        # already supports only POSIX/WSL; callers can still enforce parent ACLs.
        pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class EvidenceAppendResult:
    receipt_id: str
    receipt_sequence: int
    spool_offset: int
    evidence_id: str
    idempotency_key: str
    disposition: str
    conflict_evidence_ids: tuple[str, ...] = ()

    @property
    def inserted(self) -> bool:
        return self.disposition == "inserted"

    @property
    def duplicate(self) -> bool:
        return self.disposition == "duplicate"

    @property
    def conflict(self) -> bool:
        return self.disposition == "conflict"


@dataclass(frozen=True)
class LinkAppendResult:
    receipt_id: str
    receipt_sequence: int
    spool_offset: int
    link_id: str
    idempotency_key: str
    disposition: str
    validation_state: str
    conflict_link_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceRecord:
    envelope: EvidenceEnvelope
    first_receipt_sequence: int
    last_receipt_sequence: int
    receipt_count: int
    duplicate_receipt_count: int
    is_conflict: bool
    acknowledged: bool = False

    @property
    def evidence_id(self) -> str:
        return self.envelope.evidence_id


@dataclass(frozen=True)
class ClaimedLinkRecord:
    link: ClaimedLink
    first_receipt_sequence: int
    last_receipt_sequence: int
    receipt_count: int
    duplicate_receipt_count: int
    is_conflict: bool
    validation_state: str

    @property
    def link_id(self) -> str:
        return self.link.link_id


@dataclass(frozen=True)
class SpoolReplayResult:
    projected_receipts: int = 0
    already_projected_receipts: int = 0
    invalid_records: int = 0


@dataclass(frozen=True)
class EvidenceStoreStats:
    logical_events: int
    evidence_versions: int
    receipts: int
    duplicate_receipts: int
    conflict_groups: int
    conflict_versions: int
    acknowledgements: int
    claimed_link_versions: int
    claimed_link_receipts: int
    invalid_spool_records: int
    spool_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "logical_events": self.logical_events,
            "evidence_versions": self.evidence_versions,
            "receipts": self.receipts,
            "duplicate_receipts": self.duplicate_receipts,
            "conflict_groups": self.conflict_groups,
            "conflict_versions": self.conflict_versions,
            "acknowledgements": self.acknowledgements,
            "claimed_link_versions": self.claimed_link_versions,
            "claimed_link_receipts": self.claimed_link_receipts,
            "invalid_spool_records": self.invalid_spool_records,
            "spool_bytes": self.spool_bytes,
        }

    def __getitem__(self, key: str) -> int:
        return self.to_dict()[key]


@dataclass(frozen=True)
class RefreshableUsageItem:
    """One caller-normalized current-state usage fact.

    ``slot_key`` and ``content_hash`` are semantic identities supplied by the
    usage adapter.  The store deliberately does not derive them from the full
    envelope because refresh observations may legitimately remint transport
    timestamps and evidence ids while describing the same current fact.

    ``usage_material_hash`` is the tokens-only digest (client-log evidence links
    excluded).  When two revisions share it but differ on ``content_hash``, only
    provenance drifted and the reconcile must not treat that as usage divergence.
    """

    slot_key: str
    slot_identity: Mapping[str, Any]
    content_hash: str
    revision_id: str
    source_order: int | None
    envelope: EvidenceEnvelope
    usage_material_hash: str | None = None


@dataclass(frozen=True)
class RefreshableUsageReconcileResult:
    """Physical and logical outcome of one current-state reconcile."""

    receipt_id: str | None = None
    spool_offset: int | None = None
    inserted: int = 0
    updated: int = 0
    resurrected: int = 0
    watermarked: int = 0
    tombstoned: int = 0
    conflicts: int = 0
    unchanged: int = 0
    stale: int = 0
    existing_conflicts: int = 0

    @property
    def batch_receipt_id(self) -> str | None:
        return self.receipt_id

    @property
    def transition_count(self) -> int:
        return (
            self.inserted
            + self.updated
            + self.resurrected
            + self.watermarked
            + self.tombstoned
            + self.conflicts
        )

    @property
    def changed(self) -> bool:
        return self.transition_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "spool_offset": self.spool_offset,
            "inserted": self.inserted,
            "updated": self.updated,
            "resurrected": self.resurrected,
            "watermarked": self.watermarked,
            "tombstoned": self.tombstoned,
            "conflicts": self.conflicts,
            "unchanged": self.unchanged,
            "stale": self.stale,
            "existing_conflicts": self.existing_conflicts,
            "transition_count": self.transition_count,
            "changed": self.changed,
        }


@dataclass(frozen=True)
class RefreshableUsageHead:
    slot_key: str
    slot_identity: Mapping[str, Any]
    content_hash: str
    source_order: int | None
    current_revision_id: str | None
    last_revision_id: str
    evidence_id: str
    tombstoned: bool
    updated_receipt_id: str


@dataclass(frozen=True)
class RefreshableUsageStats:
    heads: int
    current_heads: int
    tombstoned_heads: int
    revisions: int
    current_revisions: int
    superseded_revisions: int
    conflicts: int
    batch_receipts: int
    transitions: int
    spool_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "heads": self.heads,
            "current_heads": self.current_heads,
            "tombstoned_heads": self.tombstoned_heads,
            "revisions": self.revisions,
            "current_revisions": self.current_revisions,
            "superseded_revisions": self.superseded_revisions,
            "conflicts": self.conflicts,
            "batch_receipts": self.batch_receipts,
            "transitions": self.transitions,
            "spool_bytes": self.spool_bytes,
        }

    def __getitem__(self, key: str) -> int:
        return self.to_dict()[key]


class EvidenceStore:
    """Append-only v2 evidence store with a rebuildable SQLite projection."""

    def __init__(self, root: Path | str) -> None:
        if root is None:
            raise ValueError("evidence store root is required")
        self.root = Path(root).expanduser()
        self.evidence_root = self.root / EVIDENCE_STORE_DIRNAME
        self.spool_path = self.evidence_root / EVIDENCE_SPOOL_FILENAME
        self.refreshable_usage_spool_path = self.evidence_root / REFRESHABLE_USAGE_SPOOL_FILENAME
        self.projection_path = self.evidence_root / EVIDENCE_PROJECTION_FILENAME
        self.lock_path = self.evidence_root / ".spool.lock"
        self.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(self.evidence_root, 0o700)
        # Schema creation and WAL-mode negotiation are writes too.  Serialize
        # them with append/recovery so several adapters can safely open the
        # same fresh store at once.
        with self._locked():
            self._initialize_projection()
            self._recover_unlocked()
        for private_file in (
            self.spool_path,
            self.refreshable_usage_spool_path,
            self.projection_path,
            self.lock_path,
        ):
            if private_file.exists():
                _owner_only(private_file, 0o600)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(self.evidence_root, 0o700)
        while True:
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                pass
            handle = os.fdopen(descriptor, "a+b")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            # The lock file lives inside the evidence tree that rebuild
            # activation atomically exchanges. A writer that blocked across
            # the swap now holds the ARCHIVED tree's lock inode while
            # self.lock_path resolves into the new live tree, so mutual
            # exclusion would silently be void. Re-verify identity after
            # acquisition and re-bind on mismatch.
            try:
                current = os.stat(self.lock_path)
            except OSError:
                current = None
            held = os.fstat(handle.fileno())
            if current is not None and (current.st_dev, current.st_ino) == (
                held.st_dev,
                held.st_ino,
            ):
                break
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        with handle:
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.projection_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_projection(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence_versions (
                    evidence_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    integrity_hash TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    assertion TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_system TEXT NOT NULL,
                    source_instance TEXT NOT NULL,
                    source_schema TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    source_event_id TEXT,
                    event_timestamp TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    dimensions_json TEXT NOT NULL,
                    project_id TEXT,
                    run_id TEXT,
                    client_session_id TEXT,
                    turn_id TEXT,
                    work_id TEXT,
                    section_id TEXT,
                    tool_call_id TEXT,
                    envelope_json TEXT NOT NULL,
                    is_conflict INTEGER NOT NULL DEFAULT 0 CHECK (is_conflict IN (0, 1)),
                    first_receipt_sequence INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_idempotency ON evidence_versions(idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_evidence_time ON evidence_versions(event_timestamp, evidence_id);
                CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence_versions(source_type, source_system);
                CREATE INDEX IF NOT EXISTS idx_evidence_source_arrival
                    ON evidence_versions(source_type, first_receipt_sequence DESC, evidence_id DESC);
                CREATE INDEX IF NOT EXISTS idx_evidence_event_type ON evidence_versions(event_type);
                CREATE INDEX IF NOT EXISTS idx_evidence_session ON evidence_versions(client_session_id);
                CREATE INDEX IF NOT EXISTS idx_evidence_work ON evidence_versions(work_id, section_id);

                CREATE TABLE IF NOT EXISTS evidence_dimensions (
                    evidence_id TEXT NOT NULL REFERENCES evidence_versions(evidence_id) ON DELETE CASCADE,
                    dimension TEXT NOT NULL,
                    PRIMARY KEY (evidence_id, dimension)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_dimension ON evidence_dimensions(dimension, evidence_id);

                CREATE TABLE IF NOT EXISTS evidence_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id TEXT NOT NULL UNIQUE,
                    spool_offset INTEGER NOT NULL,
                    evidence_id TEXT NOT NULL REFERENCES evidence_versions(evidence_id),
                    idempotency_key TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK (disposition IN ('inserted', 'duplicate', 'conflict')),
                    received_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_receipts_evidence ON evidence_receipts(evidence_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_receipts_idempotency ON evidence_receipts(idempotency_key, sequence);

                CREATE TABLE IF NOT EXISTS evidence_acknowledgements (
                    consumer TEXT NOT NULL,
                    evidence_id TEXT NOT NULL REFERENCES evidence_versions(evidence_id),
                    acknowledged_at TEXT NOT NULL,
                    PRIMARY KEY (consumer, evidence_id)
                );

                CREATE TABLE IF NOT EXISTS claimed_link_versions (
                    link_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    integrity_hash TEXT NOT NULL,
                    claimed_evidence_id TEXT NOT NULL,
                    observed_evidence_id TEXT NOT NULL,
                    dimensions_json TEXT NOT NULL,
                    link_json TEXT NOT NULL,
                    validation_state TEXT NOT NULL CHECK (validation_state IN ('pending', 'valid', 'invalid')),
                    is_conflict INTEGER NOT NULL DEFAULT 0 CHECK (is_conflict IN (0, 1)),
                    first_receipt_sequence INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_claimed_link_idempotency ON claimed_link_versions(idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_claimed_link_claimed ON claimed_link_versions(claimed_evidence_id);
                CREATE INDEX IF NOT EXISTS idx_claimed_link_observed ON claimed_link_versions(observed_evidence_id);

                CREATE TABLE IF NOT EXISTS claimed_link_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id TEXT NOT NULL UNIQUE,
                    spool_offset INTEGER NOT NULL,
                    link_id TEXT NOT NULL REFERENCES claimed_link_versions(link_id),
                    idempotency_key TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK (disposition IN ('inserted', 'duplicate', 'conflict')),
                    received_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claimed_link_receipts_link ON claimed_link_receipts(link_id, sequence);

                CREATE TABLE IF NOT EXISTS refreshable_usage_batch_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    spool_offset INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
                    transition_count INTEGER NOT NULL,
                    inserted_count INTEGER NOT NULL,
                    updated_count INTEGER NOT NULL,
                    resurrected_count INTEGER NOT NULL,
                    watermarked_count INTEGER NOT NULL,
                    tombstoned_count INTEGER NOT NULL,
                    conflict_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS refreshable_usage_revisions (
                    revision_id TEXT PRIMARY KEY,
                    slot_key TEXT NOT NULL,
                    slot_identity_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_order INTEGER,
                    evidence_id TEXT NOT NULL REFERENCES evidence_versions(evidence_id),
                    status TEXT NOT NULL CHECK (status IN ('current', 'superseded')),
                    created_receipt_id TEXT NOT NULL REFERENCES refreshable_usage_batch_receipts(receipt_id),
                    created_transition_id TEXT NOT NULL UNIQUE,
                    superseded_receipt_id TEXT REFERENCES refreshable_usage_batch_receipts(receipt_id),
                    superseded_transition_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_refreshable_usage_revisions_slot
                    ON refreshable_usage_revisions(slot_key, created_receipt_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_refreshable_usage_one_current_revision
                    ON refreshable_usage_revisions(slot_key) WHERE status = 'current';

                CREATE TABLE IF NOT EXISTS refreshable_usage_heads (
                    slot_key TEXT PRIMARY KEY,
                    slot_identity_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_order INTEGER,
                    current_revision_id TEXT REFERENCES refreshable_usage_revisions(revision_id),
                    last_revision_id TEXT NOT NULL REFERENCES refreshable_usage_revisions(revision_id),
                    evidence_id TEXT NOT NULL REFERENCES evidence_versions(evidence_id),
                    tombstoned INTEGER NOT NULL CHECK (tombstoned IN (0, 1)),
                    updated_receipt_id TEXT NOT NULL REFERENCES refreshable_usage_batch_receipts(receipt_id),
                    updated_transition_id TEXT NOT NULL,
                    CHECK (
                        (tombstoned = 0 AND current_revision_id IS NOT NULL)
                        OR (tombstoned = 1 AND current_revision_id IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_refreshable_usage_heads_current
                    ON refreshable_usage_heads(tombstoned, slot_key);

                CREATE TABLE IF NOT EXISTS refreshable_usage_conflicts (
                    conflict_key TEXT PRIMARY KEY,
                    slot_key TEXT NOT NULL,
                    slot_identity_json TEXT NOT NULL,
                    current_revision_id TEXT NOT NULL,
                    current_content_hash TEXT NOT NULL,
                    current_source_order INTEGER,
                    head_tombstoned INTEGER NOT NULL CHECK (head_tombstoned IN (0, 1)),
                    candidate_content_hash TEXT NOT NULL,
                    candidate_revision_id TEXT NOT NULL,
                    candidate_source_order INTEGER,
                    candidate_evidence_id TEXT NOT NULL,
                    candidate_integrity_hash TEXT NOT NULL,
                    candidate_envelope_json TEXT NOT NULL,
                    first_receipt_id TEXT NOT NULL REFERENCES refreshable_usage_batch_receipts(receipt_id),
                    first_transition_id TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_refreshable_usage_conflicts_slot
                    ON refreshable_usage_conflicts(slot_key, conflict_key);

                CREATE TABLE IF NOT EXISTS refreshable_usage_transitions (
                    transition_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL REFERENCES refreshable_usage_batch_receipts(receipt_id),
                    sequence_in_batch INTEGER NOT NULL,
                    slot_key TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (
                        action IN ('insert', 'update', 'resurrect', 'watermark', 'tombstone', 'conflict')
                    ),
                    revision_id TEXT REFERENCES refreshable_usage_revisions(revision_id),
                    conflict_key TEXT REFERENCES refreshable_usage_conflicts(conflict_key),
                    prior_revision_id TEXT,
                    evidence_id TEXT REFERENCES evidence_versions(evidence_id),
                    UNIQUE(receipt_id, sequence_in_batch)
                );
                CREATE INDEX IF NOT EXISTS idx_refreshable_usage_transitions_slot
                    ON refreshable_usage_transitions(slot_key, receipt_id);

                CREATE TABLE IF NOT EXISTS spool_errors (
                    spool_offset INTEGER NOT NULL,
                    raw_digest TEXT NOT NULL,
                    error TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    PRIMARY KEY (spool_offset, raw_digest)
                );
                """
            )
            connection.execute(
                "INSERT INTO store_metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (EVIDENCE_STORE_SCHEMA_VERSION,),
            )

    def _spool_record(self, *, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = {
            "spool_schema_version": EVIDENCE_SPOOL_SCHEMA_VERSION,
            "receipt_id": f"rcp_{uuid.uuid4().hex}",
            "received_at": _utc_now(),
            "kind": kind,
            "payload": dict(payload),
        }
        return {**body, "record_hash": canonical_digest(body)}

    def _refreshable_usage_spool_record(
        self,
        *,
        main_spool_fence: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(main_spool_fence, bool) or not isinstance(main_spool_fence, int) or main_spool_fence < 0:
            raise ValueError("main_spool_fence must be a non-negative integer")
        body = {
            "spool_schema_version": REFRESHABLE_USAGE_SPOOL_SCHEMA_VERSION,
            "receipt_id": f"rcp_{uuid.uuid4().hex}",
            "received_at": _utc_now(),
            "kind": "refreshable_usage",
            "main_spool_fence": main_spool_fence,
            "payload": dict(payload),
        }
        return {**body, "record_hash": canonical_digest(body)}

    def _append_record_to_spool(self, record: Mapping[str, Any], *, path: Path) -> int:
        serialized = canonical_json_bytes(record) + b"\n"
        existed = path.exists()
        with path.open("a+b") as handle:
            try:
                os.fchmod(handle.fileno(), 0o600)
            except OSError:
                pass
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if end:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    # Preserve a torn record verbatim and start the next valid
                    # receipt on a new line.  The replay error remains visible.
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
                    end += 1
            offset = end
            handle.seek(0, os.SEEK_END)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            self._fsync_directory(self.evidence_root)
        return offset

    def _append_spool_record(self, record: Mapping[str, Any]) -> int:
        return self._append_record_to_spool(record, path=self.spool_path)

    def _append_refreshable_usage_spool_record(self, record: Mapping[str, Any]) -> int:
        return self._append_record_to_spool(record, path=self.refreshable_usage_spool_path)

    @staticmethod
    def _stable_spool_fence(path: Path) -> int:
        """Return an EOF record boundary, terminating any torn tail first."""

        if not path.is_file():
            return 0
        with path.open("r+b") as handle:
            try:
                os.fchmod(handle.fileno(), 0o600)
            except OSError:
                pass
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if end:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    end += 1
            return end

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_spool_record(record: Any) -> tuple[str, Mapping[str, Any]]:
        if not isinstance(record, Mapping):
            raise ValueError("spool record is not an object")
        if record.get("spool_schema_version") != EVIDENCE_SPOOL_SCHEMA_VERSION:
            raise ValueError("unsupported spool schema")
        receipt_id = record.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id.startswith("rcp_"):
            raise ValueError("invalid spool receipt_id")
        kind = record.get("kind")
        if kind not in {"evidence", "claimed_link"}:
            raise ValueError("unsupported spool record kind")
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("spool payload is not an object")
        expected_hash = canonical_digest({key: value for key, value in record.items() if key != "record_hash"})
        if record.get("record_hash") != expected_hash:
            raise ValueError("spool record hash mismatch")
        normalize_timestamp(record.get("received_at"), "received_at")
        return str(kind), payload

    @staticmethod
    def _validate_refreshable_usage_spool_record(
        record: Any,
    ) -> tuple[Mapping[str, Any], int]:
        if not isinstance(record, Mapping):
            raise ValueError("refreshable usage spool record is not an object")
        if record.get("spool_schema_version") != REFRESHABLE_USAGE_SPOOL_SCHEMA_VERSION:
            raise ValueError("unsupported refreshable usage spool schema")
        receipt_id = record.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id.startswith("rcp_"):
            raise ValueError("invalid refreshable usage spool receipt_id")
        if record.get("kind") != "refreshable_usage":
            raise ValueError("unsupported refreshable usage spool record kind")
        main_spool_fence = record.get("main_spool_fence")
        if (
            isinstance(main_spool_fence, bool)
            or not isinstance(main_spool_fence, int)
            or main_spool_fence < 0
        ):
            raise ValueError("invalid refreshable usage main_spool_fence")
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("refreshable usage spool payload is not an object")
        expected_hash = canonical_digest(
            {key: value for key, value in record.items() if key != "record_hash"}
        )
        if record.get("record_hash") != expected_hash:
            raise ValueError("refreshable usage spool record hash mismatch")
        normalize_timestamp(record.get("received_at"), "received_at")
        return payload, main_spool_fence

    def append(self, envelope: EvidenceEnvelope) -> EvidenceAppendResult:
        if not isinstance(envelope, EvidenceEnvelope):
            raise TypeError("append requires EvidenceEnvelope")
        if not envelope.verify_integrity():
            raise ValueError("evidence envelope integrity check failed")
        record = self._spool_record(kind="evidence", payload=envelope.to_dict())
        with self._locked():
            self._recover_unlocked()
            offset = self._append_spool_record(record)
            result = self._project_evidence_record(record, offset)
            self._set_replay_offset(self.spool_path.stat().st_size)
            return result

    def append_many(self, envelopes: Sequence[EvidenceEnvelope]) -> tuple[EvidenceAppendResult, ...]:
        # Each receipt is independently durable.  A mid-batch failure leaves a
        # truthful committed prefix and never rolls back already-fsynced facts.
        return tuple(self.append(envelope) for envelope in envelopes)

    def append_claimed_link(self, link: ClaimedLink) -> LinkAppendResult:
        if not isinstance(link, ClaimedLink):
            raise TypeError("append_claimed_link requires ClaimedLink")
        record = self._spool_record(kind="claimed_link", payload=link.to_dict())
        with self._locked():
            self._recover_unlocked()
            offset = self._append_spool_record(record)
            result = self._project_claimed_link_record(record, offset)
            self._set_replay_offset(self.spool_path.stat().st_size)
            return result

    @staticmethod
    def _validate_refreshable_usage_revision_id(revision_id: object) -> str:
        if not isinstance(revision_id, str) or not revision_id.startswith("rurev_"):
            raise ValueError("refreshable usage revision_id must be a rurev_ sha256 identifier")
        digest = revision_id.removeprefix("rurev_")
        if len(digest) != 64:
            raise ValueError("refreshable usage revision_id must be a rurev_ sha256 identifier")
        try:
            bytes.fromhex(digest)
        except ValueError as exc:
            raise ValueError("refreshable usage revision_id must be a rurev_ sha256 identifier") from exc
        return revision_id

    @staticmethod
    def _validated_refreshable_usage_items(
        items: Sequence[RefreshableUsageItem],
    ) -> tuple[tuple[RefreshableUsageItem, str], ...]:
        validated: list[tuple[RefreshableUsageItem, str]] = []
        seen_slots: set[str] = set()
        evidence_slots: dict[str, str] = {}
        revision_slots: dict[str, str] = {}
        for item in items:
            if not isinstance(item, RefreshableUsageItem):
                raise TypeError("reconcile_refreshable_usage requires RefreshableUsageItem values")
            if not item.slot_key or "\n" in item.slot_key or len(item.slot_key) > 1024:
                raise ValueError("refreshable usage slot_key must be a non-empty single-line identifier")
            if item.slot_key in seen_slots:
                raise ValueError(f"duplicate refreshable usage slot_key in batch: {item.slot_key}")
            seen_slots.add(item.slot_key)
            if not isinstance(item.slot_identity, Mapping):
                raise TypeError("refreshable usage slot_identity must be a mapping")
            identity_json = canonical_json_bytes(item.slot_identity).decode("utf-8")
            if not item.content_hash or "\n" in item.content_hash or len(item.content_hash) > 1024:
                raise ValueError("refreshable usage content_hash must be a non-empty single-line identifier")
            EvidenceStore._validate_refreshable_usage_revision_id(item.revision_id)
            if item.source_order is not None and (
                isinstance(item.source_order, bool) or not isinstance(item.source_order, int)
            ):
                raise ValueError("refreshable usage source_order must be an integer or None")
            if not isinstance(item.envelope, EvidenceEnvelope):
                raise TypeError("refreshable usage envelope must be EvidenceEnvelope")
            if not item.envelope.verify_integrity():
                raise ValueError("refreshable usage evidence envelope integrity check failed")
            prior_slot = evidence_slots.setdefault(item.envelope.evidence_id, item.slot_key)
            if prior_slot != item.slot_key:
                raise ValueError("one refreshable usage envelope cannot represent multiple slots")
            revision_slot = revision_slots.setdefault(item.revision_id, item.slot_key)
            if revision_slot != item.slot_key:
                raise ValueError("one refreshable usage revision cannot represent multiple slots")
            validated.append((item, identity_json))
        return tuple(validated)

    def _head_usage_material_hash(
        self, connection: sqlite3.Connection, head: sqlite3.Row
    ) -> str | None:
        """Tokens-only digest of the stored head's usage, or ``None`` if it
        cannot be recomputed.  Fetched lazily (only when a conflict would
        otherwise be recorded), so the reconcile hot path pays nothing."""

        try:
            evidence_id = head["evidence_id"]
        except (IndexError, KeyError):
            return None
        if not evidence_id:
            return None
        row = connection.execute(
            "SELECT envelope_json FROM evidence_versions WHERE evidence_id = ?",
            (str(evidence_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            envelope = json.loads(row["envelope_json"])
            truth = envelope["payload"]["refreshable_usage"]["truth"]
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(truth, Mapping):
            return None
        try:
            return refreshable_usage_usage_material_digest_from_material(truth)
        except Exception:  # noqa: BLE001 - a bad stored truth simply falls back to conflict.
            return None

    def _refreshable_usage_provenance_only_drift(
        self,
        connection: sqlite3.Connection,
        head: sqlite3.Row,
        item: "RefreshableUsageItem",
    ) -> bool:
        """True when the incoming item and the stored head carry the SAME usage
        material and differ only in client-log evidence links.  Callers gate
        this behind an already-established content-hash mismatch and the absence
        of a reliable source-order ordering."""

        if item.usage_material_hash is None:
            return False
        head_material = self._head_usage_material_hash(connection, head)
        return head_material is not None and head_material == item.usage_material_hash

    @staticmethod
    def _refreshable_usage_conflict_key(
        *,
        head: sqlite3.Row,
        item: RefreshableUsageItem,
        identity_json: str,
    ) -> str:
        digest = canonical_digest(
            {
                "slot_key": item.slot_key,
                "slot_identity": json.loads(identity_json),
                "current_revision_id": str(head["last_revision_id"]),
                "head_tombstoned": bool(head["tombstoned"]),
                "candidate_revision_id": item.revision_id,
            }
        )
        return f"ruc_{digest.removeprefix('sha256:')}"

    @staticmethod
    def _refreshable_usage_transition(
        *,
        action: str,
        item: RefreshableUsageItem | None,
        identity_json: str,
        head: sqlite3.Row | None,
        conflict_key: str | None = None,
    ) -> dict[str, Any]:
        transition: dict[str, Any] = {
            "transition_id": f"rut_{uuid.uuid4().hex}",
            "action": action,
            "slot_key": item.slot_key if item is not None else str(head["slot_key"]),
            "slot_identity": json.loads(identity_json),
            "prior_revision_id": str(head["last_revision_id"]) if head is not None else None,
            "prior_content_hash": str(head["content_hash"]) if head is not None else None,
            "prior_source_order": head["source_order"] if head is not None else None,
            "prior_tombstoned": bool(head["tombstoned"]) if head is not None else None,
        }
        if action in {"insert", "update", "resurrect"}:
            assert item is not None
            transition.update(
                {
                    "revision_id": item.revision_id,
                    "content_hash": item.content_hash,
                    "source_order": item.source_order,
                    "envelope": item.envelope.to_dict(),
                }
            )
        elif action == "watermark":
            assert item is not None
            transition.update(
                {
                    "revision_id": item.revision_id,
                    "content_hash": item.content_hash,
                    "source_order": item.source_order,
                }
            )
        elif action == "conflict":
            assert item is not None and conflict_key is not None
            transition.update(
                {
                    "conflict_key": conflict_key,
                    "candidate_content_hash": item.content_hash,
                    "candidate_revision_id": item.revision_id,
                    "candidate_source_order": item.source_order,
                    "candidate_envelope": item.envelope.to_dict(),
                }
            )
        return transition

    def reconcile_refreshable_usage(
        self,
        items: Sequence[RefreshableUsageItem],
        *,
        complete: bool = False,
    ) -> RefreshableUsageReconcileResult:
        """Reconcile refreshable usage facts before writing the spool.

        Unlike generic ``append``, an unchanged refresh observation is a true
        physical no-op.  Different content advances only with a strictly newer
        integer source order.  Same-content observations with a newer order
        advance only a durable watermark, without adding Evidence versions or
        receipts.  An equal or unorderable divergent candidate is retained as
        one stable conflict; older candidates are ignored.

        ``complete=True`` means the items are the full current usage snapshot
        for the entire store, not one client/home/session subset.  Product code
        may use it only while holding the v1 writer lock over a complete ledger
        view; per-event, merge, and replay paths must remain partial.
        """

        if not isinstance(complete, bool):
            raise TypeError("complete must be a bool")
        validated = self._validated_refreshable_usage_items(items)
        transitions: list[dict[str, Any]] = []
        counts = {
            "inserted": 0,
            "updated": 0,
            "resurrected": 0,
            "watermarked": 0,
            "tombstoned": 0,
            "conflicts": 0,
            "unchanged": 0,
            "stale": 0,
            "existing_conflicts": 0,
        }
        input_slots = {item.slot_key for item, _ in validated}

        with self._locked():
            self._recover_unlocked()
            with self._connection() as connection:
                head_rows = connection.execute(
                    """
                    SELECT h.*, e.idempotency_key AS head_idempotency_key
                    FROM refreshable_usage_heads AS h
                    JOIN evidence_versions AS e ON e.evidence_id = h.evidence_id
                    ORDER BY h.slot_key
                    """
                ).fetchall()
                heads = {str(row["slot_key"]): row for row in head_rows}

                for item, identity_json in sorted(validated, key=lambda value: value[0].slot_key):
                    head = heads.get(item.slot_key)
                    if head is None:
                        transitions.append(
                            self._refreshable_usage_transition(
                                action="insert",
                                item=item,
                                identity_json=identity_json,
                                head=None,
                            )
                        )
                        counts["inserted"] += 1
                        continue
                    if str(head["slot_identity_json"]) != identity_json:
                        raise ValueError(f"refreshable usage slot identity changed for {item.slot_key}")
                    if str(head["head_idempotency_key"]) != item.envelope.idempotency_key:
                        raise ValueError(f"refreshable usage envelope identity changed for {item.slot_key}")

                    head_order = head["source_order"]
                    reliable_newer = (
                        head_order is not None
                        and item.source_order is not None
                        and item.source_order > int(head_order)
                    )
                    reliable_older = (
                        head_order is not None
                        and item.source_order is not None
                        and item.source_order < int(head_order)
                    )
                    same_content = str(head["content_hash"]) == item.content_hash
                    same_revision = str(head["last_revision_id"]) == item.revision_id
                    if same_content is not same_revision:
                        raise ValueError(
                            f"refreshable usage content/revision identity mismatch for {item.slot_key}"
                        )

                    if bool(head["tombstoned"]):
                        if reliable_newer:
                            transitions.append(
                                self._refreshable_usage_transition(
                                    action="resurrect",
                                    item=item,
                                    identity_json=identity_json,
                                    head=head,
                                )
                            )
                            counts["resurrected"] += 1
                        elif reliable_older:
                            counts["stale"] += 1
                        elif complete and same_content:
                            # A complete authoritative snapshot proves that
                            # this fact is current again.  The tombstone has no
                            # source-native order of its own, so requiring a
                            # strictly newer usage watermark here would leave
                            # an unchanged restored source permanently deleted.
                            transitions.append(
                                self._refreshable_usage_transition(
                                    action="resurrect",
                                    item=item,
                                    identity_json=identity_json,
                                    head=head,
                                )
                            )
                            counts["resurrected"] += 1
                        elif same_content:
                            counts["unchanged"] += 1
                        else:
                            conflict_key = self._refreshable_usage_conflict_key(
                                head=head,
                                item=item,
                                identity_json=identity_json,
                            )
                            exists = connection.execute(
                                "SELECT 1 FROM refreshable_usage_conflicts WHERE conflict_key = ?",
                                (conflict_key,),
                            ).fetchone()
                            if exists is None:
                                transitions.append(
                                    self._refreshable_usage_transition(
                                        action="conflict",
                                        item=item,
                                        identity_json=identity_json,
                                        head=head,
                                        conflict_key=conflict_key,
                                    )
                                )
                                counts["conflicts"] += 1
                            else:
                                counts["existing_conflicts"] += 1
                        continue

                    if same_content:
                        if reliable_newer:
                            transitions.append(
                                self._refreshable_usage_transition(
                                    action="watermark",
                                    item=item,
                                    identity_json=identity_json,
                                    head=head,
                                )
                            )
                            counts["watermarked"] += 1
                        else:
                            counts["unchanged"] += 1
                    elif reliable_newer:
                        transitions.append(
                            self._refreshable_usage_transition(
                                action="update",
                                item=item,
                                identity_json=identity_json,
                                head=head,
                            )
                        )
                        counts["updated"] += 1
                    elif reliable_older:
                        counts["stale"] += 1
                    elif self._refreshable_usage_provenance_only_drift(
                        connection, head, item
                    ):
                        # Same usage material; only the client-log evidence links
                        # drifted (e.g. an older import cited no source events).
                        # Equal-source-order evidence-link drift is not usage
                        # divergence, so refresh it as unchanged rather than
                        # parking a permanent conflict. Scoped to the live head;
                        # a tombstoned head keeps the stricter conflict path,
                        # where resurrection semantics are the concern.
                        counts["unchanged"] += 1
                    else:
                        conflict_key = self._refreshable_usage_conflict_key(
                            head=head,
                            item=item,
                            identity_json=identity_json,
                        )
                        exists = connection.execute(
                            "SELECT 1 FROM refreshable_usage_conflicts WHERE conflict_key = ?",
                            (conflict_key,),
                        ).fetchone()
                        if exists is None:
                            transitions.append(
                                self._refreshable_usage_transition(
                                    action="conflict",
                                    item=item,
                                    identity_json=identity_json,
                                    head=head,
                                    conflict_key=conflict_key,
                                )
                            )
                            counts["conflicts"] += 1
                        else:
                            counts["existing_conflicts"] += 1

                if complete:
                    for slot_key, head in heads.items():
                        if slot_key in input_slots or bool(head["tombstoned"]):
                            continue
                        transitions.append(
                            self._refreshable_usage_transition(
                                action="tombstone",
                                item=None,
                                identity_json=str(head["slot_identity_json"]),
                                head=head,
                            )
                        )
                        counts["tombstoned"] += 1

            if not transitions:
                return RefreshableUsageReconcileResult(**counts)

            summary = {
                "inserted": counts["inserted"],
                "updated": counts["updated"],
                "resurrected": counts["resurrected"],
                "watermarked": counts["watermarked"],
                "tombstoned": counts["tombstoned"],
                "conflicts": counts["conflicts"],
            }
            main_spool_fence = self._stable_spool_fence(self.spool_path)
            record = self._refreshable_usage_spool_record(
                main_spool_fence=main_spool_fence,
                payload={
                    "complete": complete,
                    "summary": summary,
                    "transitions": transitions,
                },
            )
            offset = self._append_refreshable_usage_spool_record(record)
            self._project_refreshable_usage_record(record, offset)
            self._set_refreshable_usage_replay_offset(
                self.refreshable_usage_spool_path.stat().st_size
            )
            return RefreshableUsageReconcileResult(
                receipt_id=str(record["receipt_id"]),
                spool_offset=offset,
                **counts,
            )

    def reconcile_refreshable_usage_snapshot(
        self,
        items: Sequence[RefreshableUsageItem],
        *,
        complete: bool = False,
    ) -> RefreshableUsageReconcileResult:
        """Named runtime seam for snapshot callers and fail-open injection."""

        return self.reconcile_refreshable_usage(items, complete=complete)

    def _insert_evidence_version(self, connection: sqlite3.Connection, envelope: EvidenceEnvelope, *, conflict: bool) -> None:
        subjects = envelope.subjects
        connection.execute(
            """
            INSERT INTO evidence_versions(
                evidence_id, idempotency_key, integrity_hash, schema_version,
                assertion, event_type, source_type, source_system,
                source_instance, source_schema, adapter, source_event_id,
                event_timestamp, observed_at, dimensions_json, project_id,
                run_id, client_session_id, turn_id, work_id, section_id,
                tool_call_id, envelope_json, is_conflict
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                envelope.evidence_id,
                envelope.idempotency_key,
                envelope.integrity_hash,
                envelope.schema_version,
                envelope.assertion,
                envelope.event_type,
                envelope.source_type,
                envelope.source_system,
                envelope.source_instance,
                envelope.source_schema,
                envelope.adapter,
                envelope.source_event_id,
                envelope.event_timestamp,
                envelope.observed_at,
                json.dumps(list(envelope.dimensions), separators=(",", ":")),
                subjects.project_id,
                subjects.run_id,
                subjects.client_session_id,
                subjects.turn_id,
                subjects.work_id,
                subjects.section_id,
                subjects.tool_call_id,
                canonical_json_bytes(envelope.to_dict()).decode("utf-8"),
                int(conflict),
            ),
        )
        connection.executemany(
            "INSERT INTO evidence_dimensions(evidence_id, dimension) VALUES (?, ?)",
            ((envelope.evidence_id, dimension) for dimension in envelope.dimensions),
        )

    def _project_evidence_record(self, record: Mapping[str, Any], offset: int) -> EvidenceAppendResult:
        envelope = EvidenceEnvelope.from_dict(record["payload"])
        receipt_id = str(record["receipt_id"])
        received_at = normalize_timestamp(record["received_at"], "received_at")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_receipt = connection.execute(
                    "SELECT sequence, evidence_id, idempotency_key, disposition, spool_offset FROM evidence_receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if existing_receipt is not None:
                    connection.execute("COMMIT")
                    return self._evidence_append_result(connection, existing_receipt, receipt_id)

                versions = connection.execute(
                    "SELECT evidence_id, integrity_hash FROM evidence_versions WHERE idempotency_key = ? ORDER BY evidence_id",
                    (envelope.idempotency_key,),
                ).fetchall()
                exact = next((row for row in versions if row["integrity_hash"] == envelope.integrity_hash), None)
                if exact is not None:
                    disposition = "duplicate"
                    evidence_id = str(exact["evidence_id"])
                elif versions:
                    disposition = "conflict"
                    evidence_id = envelope.evidence_id
                    self._insert_evidence_version(connection, envelope, conflict=True)
                    connection.execute(
                        "UPDATE evidence_versions SET is_conflict = 1 WHERE idempotency_key = ?",
                        (envelope.idempotency_key,),
                    )
                else:
                    disposition = "inserted"
                    evidence_id = envelope.evidence_id
                    self._insert_evidence_version(connection, envelope, conflict=False)

                cursor = connection.execute(
                    """
                    INSERT INTO evidence_receipts(
                        receipt_id, spool_offset, evidence_id, idempotency_key,
                        disposition, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (receipt_id, offset, evidence_id, envelope.idempotency_key, disposition, received_at),
                )
                sequence = int(cursor.lastrowid)
                connection.execute(
                    "UPDATE evidence_versions SET first_receipt_sequence = COALESCE(first_receipt_sequence, ?) WHERE evidence_id = ?",
                    (sequence, evidence_id),
                )
                self._refresh_link_states(connection, evidence_id)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            row = connection.execute(
                "SELECT sequence, evidence_id, idempotency_key, disposition, spool_offset FROM evidence_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            assert row is not None
            return self._evidence_append_result(connection, row, receipt_id)

    @staticmethod
    def _evidence_append_result(connection: sqlite3.Connection, row: sqlite3.Row, receipt_id: str) -> EvidenceAppendResult:
        conflicts = connection.execute(
            "SELECT evidence_id FROM evidence_versions WHERE idempotency_key = ? AND is_conflict = 1 ORDER BY evidence_id",
            (row["idempotency_key"],),
        ).fetchall()
        return EvidenceAppendResult(
            receipt_id=receipt_id,
            receipt_sequence=int(row["sequence"]),
            spool_offset=int(row["spool_offset"]),
            evidence_id=str(row["evidence_id"]),
            idempotency_key=str(row["idempotency_key"]),
            disposition=str(row["disposition"]),
            conflict_evidence_ids=tuple(str(conflict["evidence_id"]) for conflict in conflicts),
        )

    def _insert_refreshable_usage_evidence_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        envelope: EvidenceEnvelope,
        transition_id: str,
        spool_offset: int,
        received_at: str,
        conflict: bool = False,
    ) -> None:
        """Project one revision from the transition spool into Evidence.

        For ``rrc_`` receipts, ``evidence_receipts.spool_offset`` is relative
        to ``refreshable-usage.jsonl``.  Ordinary ``rcp_`` Evidence receipts
        continue to use offsets in the legacy ``spool.jsonl``.
        """

        existing_version = connection.execute(
            "SELECT integrity_hash, idempotency_key FROM evidence_versions WHERE evidence_id = ?",
            (envelope.evidence_id,),
        ).fetchone()
        if existing_version is None:
            self._insert_evidence_version(connection, envelope, conflict=conflict)
            disposition = "conflict" if conflict else "inserted"
        else:
            if (
                str(existing_version["integrity_hash"]) != envelope.integrity_hash
                or str(existing_version["idempotency_key"]) != envelope.idempotency_key
            ):
                raise ValueError("refreshable usage evidence id collision")
            disposition = "conflict" if conflict else "duplicate"
        if conflict:
            connection.execute(
                "UPDATE evidence_versions SET is_conflict = 1 WHERE idempotency_key = ?",
                (envelope.idempotency_key,),
            )

        digest = canonical_digest({"refreshable_usage_transition": transition_id})
        evidence_receipt_id = f"rrc_{digest.removeprefix('sha256:')}"
        existing_receipt = connection.execute(
            "SELECT evidence_id FROM evidence_receipts WHERE receipt_id = ?",
            (evidence_receipt_id,),
        ).fetchone()
        if existing_receipt is not None:
            if str(existing_receipt["evidence_id"]) != envelope.evidence_id:
                raise ValueError("refreshable usage evidence receipt collision")
            return
        cursor = connection.execute(
            """
            INSERT INTO evidence_receipts(
                receipt_id, spool_offset, evidence_id, idempotency_key,
                disposition, received_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_receipt_id,
                spool_offset,
                envelope.evidence_id,
                envelope.idempotency_key,
                disposition,
                received_at,
            ),
        )
        sequence = int(cursor.lastrowid)
        connection.execute(
            "UPDATE evidence_versions SET first_receipt_sequence = COALESCE(first_receipt_sequence, ?) WHERE evidence_id = ?",
            (sequence, envelope.evidence_id),
        )
        self._refresh_link_states(connection, envelope.evidence_id)

    @staticmethod
    def _refreshable_usage_batch_result(
        row: sqlite3.Row,
        *,
        unchanged: int = 0,
        stale: int = 0,
        existing_conflicts: int = 0,
    ) -> RefreshableUsageReconcileResult:
        return RefreshableUsageReconcileResult(
            receipt_id=str(row["receipt_id"]),
            spool_offset=int(row["spool_offset"]),
            inserted=int(row["inserted_count"]),
            updated=int(row["updated_count"]),
            resurrected=int(row["resurrected_count"]),
            watermarked=int(row["watermarked_count"]),
            tombstoned=int(row["tombstoned_count"]),
            conflicts=int(row["conflict_count"]),
            unchanged=unchanged,
            stale=stale,
            existing_conflicts=existing_conflicts,
        )

    def _project_refreshable_usage_record(
        self,
        record: Mapping[str, Any],
        offset: int,
    ) -> RefreshableUsageReconcileResult:
        payload = record["payload"]
        if not isinstance(payload, Mapping):
            raise ValueError("refreshable usage spool payload is not an object")
        complete = payload.get("complete")
        if not isinstance(complete, bool):
            raise ValueError("refreshable usage complete flag must be a bool")
        summary = payload.get("summary")
        transitions = payload.get("transitions")
        if not isinstance(summary, Mapping):
            raise ValueError("refreshable usage summary is not an object")
        if not isinstance(transitions, list) or not transitions:
            raise ValueError("refreshable usage transitions must be a non-empty list")
        summary_keys = (
            "inserted",
            "updated",
            "resurrected",
            "watermarked",
            "tombstoned",
            "conflicts",
        )
        summary_counts: dict[str, int] = {}
        for key in summary_keys:
            value = summary.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"refreshable usage summary {key} must be a non-negative integer")
            summary_counts[key] = value
        action_to_summary = {
            "insert": "inserted",
            "update": "updated",
            "resurrect": "resurrected",
            "watermark": "watermarked",
            "tombstone": "tombstoned",
            "conflict": "conflicts",
        }
        actual_counts = {key: 0 for key in summary_keys}
        transition_ids: set[str] = set()
        for transition in transitions:
            if not isinstance(transition, Mapping):
                raise ValueError("refreshable usage transition is not an object")
            action = transition.get("action")
            if action not in action_to_summary:
                raise ValueError("unsupported refreshable usage transition action")
            transition_id = transition.get("transition_id")
            if not isinstance(transition_id, str) or not transition_id.startswith("rut_"):
                raise ValueError("invalid refreshable usage transition id")
            if transition_id in transition_ids:
                raise ValueError("duplicate refreshable usage transition id")
            transition_ids.add(transition_id)
            actual_counts[action_to_summary[str(action)]] += 1
        if actual_counts != summary_counts:
            raise ValueError("refreshable usage transition summary mismatch")

        receipt_id = str(record["receipt_id"])
        received_at = normalize_timestamp(record["received_at"], "received_at")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_batch = connection.execute(
                    "SELECT * FROM refreshable_usage_batch_receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if existing_batch is not None:
                    connection.execute("COMMIT")
                    return self._refreshable_usage_batch_result(existing_batch)

                connection.execute(
                    """
                    INSERT INTO refreshable_usage_batch_receipts(
                        receipt_id, spool_offset, received_at, complete,
                        transition_count, inserted_count, updated_count,
                        resurrected_count, watermarked_count,
                        tombstoned_count, conflict_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        offset,
                        received_at,
                        int(complete),
                        len(transitions),
                        summary_counts["inserted"],
                        summary_counts["updated"],
                        summary_counts["resurrected"],
                        summary_counts["watermarked"],
                        summary_counts["tombstoned"],
                        summary_counts["conflicts"],
                    ),
                )

                for sequence, transition in enumerate(transitions):
                    assert isinstance(transition, Mapping)
                    transition_id = str(transition["transition_id"])
                    action = str(transition["action"])
                    slot_key = transition.get("slot_key")
                    if not isinstance(slot_key, str) or not slot_key:
                        raise ValueError("invalid refreshable usage transition slot_key")
                    slot_identity = transition.get("slot_identity")
                    if not isinstance(slot_identity, Mapping):
                        raise ValueError("invalid refreshable usage transition slot_identity")
                    identity_json = canonical_json_bytes(slot_identity).decode("utf-8")
                    prior_revision_id = transition.get("prior_revision_id")
                    prior_content_hash = transition.get("prior_content_hash")
                    prior_source_order = transition.get("prior_source_order")
                    prior_tombstoned = transition.get("prior_tombstoned")
                    if prior_source_order is not None and (
                        isinstance(prior_source_order, bool) or not isinstance(prior_source_order, int)
                    ):
                        raise ValueError("invalid refreshable usage prior source order")

                    head = connection.execute(
                        """
                        SELECT h.*, e.idempotency_key AS head_idempotency_key
                        FROM refreshable_usage_heads AS h
                        JOIN evidence_versions AS e ON e.evidence_id = h.evidence_id
                        WHERE h.slot_key = ?
                        """,
                        (slot_key,),
                    ).fetchone()
                    if action == "insert":
                        if head is not None or any(
                            value is not None
                            for value in (prior_revision_id, prior_content_hash, prior_source_order, prior_tombstoned)
                        ):
                            raise ValueError("refreshable usage insert precondition failed")
                    else:
                        if head is None:
                            raise ValueError("refreshable usage transition head is missing")
                        if str(head["slot_identity_json"]) != identity_json:
                            raise ValueError("refreshable usage transition slot identity mismatch")
                        if (
                            str(head["last_revision_id"]) != prior_revision_id
                            or str(head["content_hash"]) != prior_content_hash
                            or head["source_order"] != prior_source_order
                            or bool(head["tombstoned"]) is not prior_tombstoned
                        ):
                            raise ValueError("refreshable usage transition head precondition failed")
                        if action in {"update", "watermark", "tombstone"} and bool(head["tombstoned"]):
                            raise ValueError("refreshable usage current-head transition requires a live head")
                        if action == "resurrect" and not bool(head["tombstoned"]):
                            raise ValueError("refreshable usage resurrection requires a tombstoned head")

                    revision_id: str | None = None
                    conflict_key: str | None = None
                    evidence_id: str | None = None
                    if action in {"insert", "update", "resurrect"}:
                        revision_value = transition.get("revision_id")
                        content_hash = transition.get("content_hash")
                        source_order = transition.get("source_order")
                        envelope_value = transition.get("envelope")
                        revision_value = self._validate_refreshable_usage_revision_id(revision_value)
                        if not isinstance(content_hash, str) or not content_hash:
                            raise ValueError("invalid refreshable usage revision content hash")
                        if source_order is not None and (
                            isinstance(source_order, bool) or not isinstance(source_order, int)
                        ):
                            raise ValueError("invalid refreshable usage revision source order")
                        if not isinstance(envelope_value, Mapping):
                            raise ValueError("invalid refreshable usage revision envelope")
                        envelope = EvidenceEnvelope.from_dict(envelope_value)
                        if not envelope.verify_integrity():
                            raise ValueError("refreshable usage revision envelope integrity check failed")
                        if head is not None and str(head["head_idempotency_key"]) != envelope.idempotency_key:
                            raise ValueError("refreshable usage revision envelope identity changed")
                        self._insert_refreshable_usage_evidence_receipt(
                            connection,
                            envelope=envelope,
                            transition_id=transition_id,
                            spool_offset=offset,
                            received_at=received_at,
                        )
                        evidence_id = envelope.evidence_id
                        revision_id = revision_value

                        if head is not None:
                            same_prior_content = str(head["content_hash"]) == content_hash
                            same_prior_revision = str(head["last_revision_id"]) == revision_id
                            if same_prior_content is not same_prior_revision:
                                raise ValueError("refreshable usage content/revision identity mismatch")
                            if action == "update" and same_prior_revision:
                                raise ValueError("refreshable usage update must advance to a different revision")

                        if action == "update":
                            cursor = connection.execute(
                                """
                                UPDATE refreshable_usage_revisions
                                SET status = 'superseded', superseded_receipt_id = ?,
                                    superseded_transition_id = ?
                                WHERE revision_id = ? AND status = 'current'
                                """,
                                (receipt_id, transition_id, prior_revision_id),
                            )
                            if cursor.rowcount != 1:
                                raise ValueError("refreshable usage prior current revision is missing")
                        elif action == "resurrect":
                            prior = connection.execute(
                                "SELECT status FROM refreshable_usage_revisions WHERE revision_id = ?",
                                (prior_revision_id,),
                            ).fetchone()
                            if prior is None or str(prior["status"]) != "superseded":
                                raise ValueError("refreshable usage tombstone revision history is inconsistent")

                        target_revision = connection.execute(
                            "SELECT * FROM refreshable_usage_revisions WHERE revision_id = ?",
                            (revision_id,),
                        ).fetchone()
                        if action == "insert" and target_revision is not None:
                            raise ValueError("refreshable usage insert revision already exists")
                        if target_revision is None:
                            connection.execute(
                                """
                                INSERT INTO refreshable_usage_revisions(
                                    revision_id, slot_key, slot_identity_json,
                                    content_hash, source_order, evidence_id, status,
                                    created_receipt_id, created_transition_id
                                ) VALUES (?, ?, ?, ?, ?, ?, 'current', ?, ?)
                                """,
                                (
                                    revision_id,
                                    slot_key,
                                    identity_json,
                                    content_hash,
                                    source_order,
                                    evidence_id,
                                    receipt_id,
                                    transition_id,
                                ),
                            )
                        else:
                            if (
                                str(target_revision["slot_key"]) != slot_key
                                or str(target_revision["slot_identity_json"]) != identity_json
                                or str(target_revision["content_hash"]) != content_hash
                                or str(target_revision["status"]) != "superseded"
                            ):
                                raise ValueError("refreshable usage stable revision collision")
                            cursor = connection.execute(
                                """
                                UPDATE refreshable_usage_revisions
                                SET status = 'current', source_order = ?,
                                    superseded_receipt_id = NULL,
                                    superseded_transition_id = NULL
                                WHERE revision_id = ? AND status = 'superseded'
                                """,
                                (source_order, revision_id),
                            )
                            if cursor.rowcount != 1:
                                raise ValueError("refreshable usage stable revision reactivation failed")
                        if action == "insert":
                            connection.execute(
                                """
                                INSERT INTO refreshable_usage_heads(
                                    slot_key, slot_identity_json, content_hash,
                                    source_order, current_revision_id,
                                    last_revision_id, evidence_id, tombstoned,
                                    updated_receipt_id, updated_transition_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                                """,
                                (
                                    slot_key,
                                    identity_json,
                                    content_hash,
                                    source_order,
                                    revision_id,
                                    revision_id,
                                    evidence_id,
                                    receipt_id,
                                    transition_id,
                                ),
                            )
                        else:
                            connection.execute(
                                """
                                UPDATE refreshable_usage_heads
                                SET content_hash = ?, source_order = ?,
                                    current_revision_id = ?, last_revision_id = ?,
                                    evidence_id = ?, tombstoned = 0,
                                    updated_receipt_id = ?, updated_transition_id = ?
                                WHERE slot_key = ?
                                """,
                                (
                                    content_hash,
                                    source_order,
                                    revision_id,
                                    revision_id,
                                    evidence_id,
                                    receipt_id,
                                    transition_id,
                                    slot_key,
                                ),
                            )
                    elif action == "watermark":
                        assert head is not None
                        revision_id = self._validate_refreshable_usage_revision_id(
                            transition.get("revision_id")
                        )
                        content_hash = transition.get("content_hash")
                        source_order = transition.get("source_order")
                        if not isinstance(content_hash, str) or not content_hash:
                            raise ValueError("invalid refreshable usage watermark content hash")
                        if (
                            isinstance(source_order, bool)
                            or not isinstance(source_order, int)
                            or prior_source_order is None
                            or source_order <= prior_source_order
                        ):
                            raise ValueError("refreshable usage watermark must be strictly newer")
                        if (
                            revision_id != prior_revision_id
                            or content_hash != prior_content_hash
                            or str(head["current_revision_id"]) != revision_id
                        ):
                            raise ValueError("refreshable usage watermark revision mismatch")
                        cursor = connection.execute(
                            """
                            UPDATE refreshable_usage_revisions
                            SET source_order = ?
                            WHERE revision_id = ? AND slot_key = ?
                              AND content_hash = ? AND status = 'current'
                            """,
                            (source_order, revision_id, slot_key, content_hash),
                        )
                        if cursor.rowcount != 1:
                            raise ValueError("refreshable usage watermark current revision is missing")
                        connection.execute(
                            """
                            UPDATE refreshable_usage_heads
                            SET source_order = ?, updated_receipt_id = ?,
                                updated_transition_id = ?
                            WHERE slot_key = ?
                            """,
                            (source_order, receipt_id, transition_id, slot_key),
                        )
                        evidence_id = str(head["evidence_id"])
                    elif action == "tombstone":
                        assert head is not None
                        cursor = connection.execute(
                            """
                            UPDATE refreshable_usage_revisions
                            SET status = 'superseded', superseded_receipt_id = ?,
                                superseded_transition_id = ?
                            WHERE revision_id = ? AND status = 'current'
                            """,
                            (receipt_id, transition_id, prior_revision_id),
                        )
                        if cursor.rowcount != 1:
                            raise ValueError("refreshable usage tombstone current revision is missing")
                        connection.execute(
                            """
                            UPDATE refreshable_usage_heads
                            SET current_revision_id = NULL, tombstoned = 1,
                                updated_receipt_id = ?, updated_transition_id = ?
                            WHERE slot_key = ?
                            """,
                            (receipt_id, transition_id, slot_key),
                        )
                        evidence_id = str(head["evidence_id"])
                    else:
                        assert action == "conflict" and head is not None
                        conflict_value = transition.get("conflict_key")
                        candidate_content_hash = transition.get("candidate_content_hash")
                        candidate_revision_id = transition.get("candidate_revision_id")
                        candidate_source_order = transition.get("candidate_source_order")
                        candidate_envelope_value = transition.get("candidate_envelope")
                        if not isinstance(conflict_value, str) or not conflict_value.startswith("ruc_"):
                            raise ValueError("invalid refreshable usage conflict key")
                        if not isinstance(candidate_content_hash, str) or not candidate_content_hash:
                            raise ValueError("invalid refreshable usage conflict content hash")
                        candidate_revision_id = self._validate_refreshable_usage_revision_id(
                            candidate_revision_id
                        )
                        if candidate_source_order is not None and (
                            isinstance(candidate_source_order, bool) or not isinstance(candidate_source_order, int)
                        ):
                            raise ValueError("invalid refreshable usage conflict source order")
                        if not isinstance(candidate_envelope_value, Mapping):
                            raise ValueError("invalid refreshable usage conflict envelope")
                        candidate_envelope = EvidenceEnvelope.from_dict(candidate_envelope_value)
                        if not candidate_envelope.verify_integrity():
                            raise ValueError("refreshable usage conflict envelope integrity check failed")
                        if str(head["head_idempotency_key"]) != candidate_envelope.idempotency_key:
                            raise ValueError("refreshable usage conflict envelope identity changed")
                        if (
                            str(head["content_hash"]) == candidate_content_hash
                            or str(head["last_revision_id"]) == candidate_revision_id
                        ):
                            raise ValueError("refreshable usage conflict must describe a distinct revision")
                        expected_conflict_key = self._refreshable_usage_conflict_key(
                            head=head,
                            item=RefreshableUsageItem(
                                slot_key=slot_key,
                                slot_identity=slot_identity,
                                content_hash=candidate_content_hash,
                                revision_id=candidate_revision_id,
                                source_order=candidate_source_order,
                                envelope=candidate_envelope,
                            ),
                            identity_json=identity_json,
                        )
                        if conflict_value != expected_conflict_key:
                            raise ValueError("refreshable usage conflict key mismatch")
                        conflict_key = conflict_value
                        self._insert_refreshable_usage_evidence_receipt(
                            connection,
                            envelope=candidate_envelope,
                            transition_id=transition_id,
                            spool_offset=offset,
                            received_at=received_at,
                            conflict=True,
                        )
                        evidence_id = candidate_envelope.evidence_id
                        connection.execute(
                            """
                            INSERT INTO refreshable_usage_conflicts(
                                conflict_key, slot_key, slot_identity_json,
                                current_revision_id, current_content_hash,
                                current_source_order, head_tombstoned,
                                candidate_content_hash, candidate_revision_id,
                                candidate_source_order,
                                candidate_evidence_id, candidate_integrity_hash,
                                candidate_envelope_json, first_receipt_id,
                                first_transition_id
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                conflict_key,
                                slot_key,
                                identity_json,
                                str(head["last_revision_id"]),
                                str(head["content_hash"]),
                                head["source_order"],
                                int(bool(head["tombstoned"])),
                                candidate_content_hash,
                                candidate_revision_id,
                                candidate_source_order,
                                candidate_envelope.evidence_id,
                                candidate_envelope.integrity_hash,
                                canonical_json_bytes(candidate_envelope.to_dict()).decode("utf-8"),
                                receipt_id,
                                transition_id,
                            ),
                        )

                    connection.execute(
                        """
                        INSERT INTO refreshable_usage_transitions(
                            transition_id, receipt_id, sequence_in_batch,
                            slot_key, action, revision_id, conflict_key,
                            prior_revision_id, evidence_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            transition_id,
                            receipt_id,
                            sequence,
                            slot_key,
                            action,
                            revision_id,
                            conflict_key,
                            prior_revision_id,
                            evidence_id,
                        ),
                    )

                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            batch = connection.execute(
                "SELECT * FROM refreshable_usage_batch_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            assert batch is not None
            return self._refreshable_usage_batch_result(batch)

    @staticmethod
    def _link_validation_state(connection: sqlite3.Connection, link: ClaimedLink) -> str:
        rows = connection.execute(
            "SELECT evidence_id, assertion, dimensions_json FROM evidence_versions WHERE evidence_id IN (?, ?)",
            (link.claimed_evidence_id, link.observed_evidence_id),
        ).fetchall()
        by_id = {str(row["evidence_id"]): row for row in rows}
        claimed = by_id.get(link.claimed_evidence_id)
        observed = by_id.get(link.observed_evidence_id)
        if claimed is None or observed is None:
            return "pending"
        if claimed["assertion"] != "claimed" or observed["assertion"] != "observed":
            return "invalid"
        claimed_dimensions = set(json.loads(claimed["dimensions_json"]))
        observed_dimensions = set(json.loads(observed["dimensions_json"]))
        if not set(link.dimensions).issubset(claimed_dimensions & observed_dimensions):
            return "invalid"
        return "valid"

    def _refresh_link_states(self, connection: sqlite3.Connection, evidence_id: str) -> None:
        rows = connection.execute(
            "SELECT link_json FROM claimed_link_versions WHERE claimed_evidence_id = ? OR observed_evidence_id = ?",
            (evidence_id, evidence_id),
        ).fetchall()
        for row in rows:
            link = ClaimedLink.from_dict(json.loads(row["link_json"]))
            state = self._link_validation_state(connection, link)
            connection.execute("UPDATE claimed_link_versions SET validation_state = ? WHERE link_id = ?", (state, link.link_id))

    def _project_claimed_link_record(self, record: Mapping[str, Any], offset: int) -> LinkAppendResult:
        link = ClaimedLink.from_dict(record["payload"])
        receipt_id = str(record["receipt_id"])
        received_at = normalize_timestamp(record["received_at"], "received_at")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_receipt = connection.execute(
                    "SELECT sequence, link_id, idempotency_key, disposition, spool_offset FROM claimed_link_receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if existing_receipt is not None:
                    connection.execute("COMMIT")
                    return self._link_append_result(connection, existing_receipt, receipt_id)
                versions = connection.execute(
                    "SELECT link_id, integrity_hash FROM claimed_link_versions WHERE idempotency_key = ? ORDER BY link_id",
                    (link.idempotency_key,),
                ).fetchall()
                exact = next((row for row in versions if row["integrity_hash"] == link.integrity_hash), None)
                if exact is not None:
                    disposition = "duplicate"
                    link_id = str(exact["link_id"])
                else:
                    disposition = "conflict" if versions else "inserted"
                    link_id = link.link_id
                    state = self._link_validation_state(connection, link)
                    connection.execute(
                        """
                        INSERT INTO claimed_link_versions(
                            link_id, idempotency_key, integrity_hash,
                            claimed_evidence_id, observed_evidence_id,
                            dimensions_json, link_json, validation_state,
                            is_conflict
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            link.link_id,
                            link.idempotency_key,
                            link.integrity_hash,
                            link.claimed_evidence_id,
                            link.observed_evidence_id,
                            json.dumps(list(link.dimensions), separators=(",", ":")),
                            canonical_json_bytes(link.to_dict()).decode("utf-8"),
                            state,
                            int(bool(versions)),
                        ),
                    )
                    if versions:
                        connection.execute(
                            "UPDATE claimed_link_versions SET is_conflict = 1 WHERE idempotency_key = ?",
                            (link.idempotency_key,),
                        )
                cursor = connection.execute(
                    """
                    INSERT INTO claimed_link_receipts(
                        receipt_id, spool_offset, link_id, idempotency_key,
                        disposition, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (receipt_id, offset, link_id, link.idempotency_key, disposition, received_at),
                )
                sequence = int(cursor.lastrowid)
                connection.execute(
                    "UPDATE claimed_link_versions SET first_receipt_sequence = COALESCE(first_receipt_sequence, ?) WHERE link_id = ?",
                    (sequence, link_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            row = connection.execute(
                "SELECT sequence, link_id, idempotency_key, disposition, spool_offset FROM claimed_link_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            assert row is not None
            return self._link_append_result(connection, row, receipt_id)

    @staticmethod
    def _link_append_result(connection: sqlite3.Connection, row: sqlite3.Row, receipt_id: str) -> LinkAppendResult:
        conflicts = connection.execute(
            "SELECT link_id FROM claimed_link_versions WHERE idempotency_key = ? AND is_conflict = 1 ORDER BY link_id",
            (row["idempotency_key"],),
        ).fetchall()
        state = connection.execute(
            "SELECT validation_state FROM claimed_link_versions WHERE link_id = ?",
            (row["link_id"],),
        ).fetchone()
        assert state is not None
        return LinkAppendResult(
            receipt_id=receipt_id,
            receipt_sequence=int(row["sequence"]),
            spool_offset=int(row["spool_offset"]),
            link_id=str(row["link_id"]),
            idempotency_key=str(row["idempotency_key"]),
            disposition=str(row["disposition"]),
            validation_state=str(state["validation_state"]),
            conflict_link_ids=tuple(str(conflict["link_id"]) for conflict in conflicts),
        )

    def _record_spool_error(
        self,
        offset: int,
        raw: bytes,
        error: BaseException,
        *,
        spool_name: str = EVIDENCE_SPOOL_FILENAME,
    ) -> None:
        digest_material = {"raw_sha256": hashlib.sha256(raw).hexdigest()}
        if spool_name != EVIDENCE_SPOOL_FILENAME:
            digest_material["spool"] = spool_name
        digest = canonical_digest(digest_material)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO spool_errors(spool_offset, raw_digest, error, detected_at)
                VALUES (?, ?, ?, ?)
                """,
                (offset, digest, f"{type(error).__name__}: {error}"[:1000], _utc_now()),
            )

    def _stored_replay_offset(self, key: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM store_metadata WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return 0
        try:
            value = int(row["value"])
        except (TypeError, ValueError):
            return 0
        return max(0, value)

    def _set_stored_replay_offset(self, key: str, offset: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO store_metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(max(0, offset))),
            )

    def _replay_offset(self) -> int:
        return self._stored_replay_offset("replay_offset")

    def _set_replay_offset(self, offset: int) -> None:
        self._set_stored_replay_offset("replay_offset", offset)

    def _refreshable_usage_replay_offset(self) -> int:
        return self._stored_replay_offset("refreshable_usage_replay_offset")

    def _set_refreshable_usage_replay_offset(self, offset: int) -> None:
        self._set_stored_replay_offset("refreshable_usage_replay_offset", offset)

    def _project_main_spool_raw(self, raw: bytes, offset: int) -> SpoolReplayResult:
        if not raw.strip():
            return SpoolReplayResult()
        try:
            record = json.loads(raw)
            kind, _ = self._validate_spool_record(record)
            receipt_id = str(record["receipt_id"])
            with self._connection() as connection:
                table = "evidence_receipts" if kind == "evidence" else "claimed_link_receipts"
                exists = connection.execute(
                    f"SELECT 1 FROM {table} WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone() is not None
            if exists:
                return SpoolReplayResult(already_projected_receipts=1)
            if kind == "evidence":
                self._project_evidence_record(record, offset)
            else:
                self._project_claimed_link_record(record, offset)
            return SpoolReplayResult(projected_receipts=1)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
            KeyError,
            sqlite3.DatabaseError,
        ) as exc:
            self._record_spool_error(offset, raw, exc)
            return SpoolReplayResult(invalid_records=1)

    def _project_refreshable_usage_spool_raw(
        self,
        record: Mapping[str, Any],
        raw: bytes,
        offset: int,
    ) -> SpoolReplayResult:
        try:
            receipt_id = str(record["receipt_id"])
            with self._connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM refreshable_usage_batch_receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone() is not None
            if exists:
                return SpoolReplayResult(already_projected_receipts=1)
            self._project_refreshable_usage_record(record, offset)
            return SpoolReplayResult(projected_receipts=1)
        except (TypeError, ValueError, KeyError, sqlite3.DatabaseError) as exc:
            self._record_spool_error(
                offset,
                raw,
                exc,
                spool_name=REFRESHABLE_USAGE_SPOOL_FILENAME,
            )
            return SpoolReplayResult(invalid_records=1)

    @staticmethod
    def _add_replay_result(left: SpoolReplayResult, right: SpoolReplayResult) -> SpoolReplayResult:
        return SpoolReplayResult(
            projected_receipts=left.projected_receipts + right.projected_receipts,
            already_projected_receipts=(
                left.already_projected_receipts + right.already_projected_receipts
            ),
            invalid_records=left.invalid_records + right.invalid_records,
        )

    def _recover_unlocked(self) -> SpoolReplayResult:
        """Replay both spools in their original shared-receipt arrival order."""

        main_size = self.spool_path.stat().st_size if self.spool_path.is_file() else 0
        refreshable_size = (
            self.refreshable_usage_spool_path.stat().st_size
            if self.refreshable_usage_spool_path.is_file()
            else 0
        )
        main_offset = self._replay_offset()
        refreshable_offset = self._refreshable_usage_replay_offset()
        main_offset = main_offset if main_offset <= main_size else 0
        refreshable_offset = refreshable_offset if refreshable_offset <= refreshable_size else 0
        result = SpoolReplayResult()

        with ExitStack() as stack:
            main_handle = (
                stack.enter_context(self.spool_path.open("rb"))
                if self.spool_path.is_file()
                else None
            )
            refreshable_handle = (
                stack.enter_context(self.refreshable_usage_spool_path.open("rb"))
                if self.refreshable_usage_spool_path.is_file()
                else None
            )
            if main_handle is not None:
                main_handle.seek(main_offset)
            if refreshable_handle is not None:
                refreshable_handle.seek(refreshable_offset)

            while refreshable_handle is not None:
                refreshable_record_offset = refreshable_handle.tell()
                refreshable_raw = refreshable_handle.readline()
                if not refreshable_raw:
                    break
                if not refreshable_raw.strip():
                    self._set_refreshable_usage_replay_offset(refreshable_handle.tell())
                    continue
                try:
                    refreshable_record = json.loads(refreshable_raw)
                    _, main_spool_fence = self._validate_refreshable_usage_spool_record(
                        refreshable_record
                    )
                except (
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    TypeError,
                    ValueError,
                    KeyError,
                ) as exc:
                    self._record_spool_error(
                        refreshable_record_offset,
                        refreshable_raw,
                        exc,
                        spool_name=REFRESHABLE_USAGE_SPOOL_FILENAME,
                    )
                    result = self._add_replay_result(
                        result,
                        SpoolReplayResult(invalid_records=1),
                    )
                    self._set_refreshable_usage_replay_offset(refreshable_handle.tell())
                    continue

                if main_spool_fence > main_size:
                    raise RuntimeError(
                        "refreshable usage spool requires missing legacy spool bytes"
                    )
                if main_handle is None and main_spool_fence != 0:
                    raise RuntimeError(
                        "refreshable usage spool requires a missing legacy spool"
                    )
                if main_handle is not None and main_handle.tell() > main_spool_fence:
                    receipt_id = str(refreshable_record["receipt_id"])
                    with self._connection() as connection:
                        already_projected = connection.execute(
                            "SELECT 1 FROM refreshable_usage_batch_receipts WHERE receipt_id = ?",
                            (receipt_id,),
                        ).fetchone() is not None
                    if not already_projected:
                        raise RuntimeError(
                            "cannot preserve refreshable usage receipt arrival order"
                        )

                while main_handle is not None and main_handle.tell() < main_spool_fence:
                    main_record_offset = main_handle.tell()
                    main_raw = main_handle.readline()
                    if not main_raw:
                        raise RuntimeError(
                            "refreshable usage spool fence exceeds readable legacy spool"
                        )
                    if main_handle.tell() > main_spool_fence:
                        raise RuntimeError(
                            "refreshable usage spool fence is not a legacy record boundary"
                        )
                    result = self._add_replay_result(
                        result,
                        self._project_main_spool_raw(main_raw, main_record_offset),
                    )
                    self._set_replay_offset(main_handle.tell())

                result = self._add_replay_result(
                    result,
                    self._project_refreshable_usage_spool_raw(
                        refreshable_record,
                        refreshable_raw,
                        refreshable_record_offset,
                    ),
                )
                self._set_refreshable_usage_replay_offset(refreshable_handle.tell())

            while main_handle is not None:
                main_record_offset = main_handle.tell()
                main_raw = main_handle.readline()
                if not main_raw:
                    break
                result = self._add_replay_result(
                    result,
                    self._project_main_spool_raw(main_raw, main_record_offset),
                )
                self._set_replay_offset(main_handle.tell())

        return result

    def recover(self) -> SpoolReplayResult:
        """Replay both append-only spools into the shared projection."""

        with self._locked():
            return self._recover_unlocked()

    replay_spool = recover

    def get(self, evidence_id: str) -> EvidenceEnvelope | None:
        with self._connection() as connection:
            row = connection.execute("SELECT envelope_json FROM evidence_versions WHERE evidence_id = ?", (evidence_id,)).fetchone()
        return EvidenceEnvelope.from_dict(json.loads(row["envelope_json"])) if row is not None else None

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> EvidenceRecord:
        return EvidenceRecord(
            envelope=EvidenceEnvelope.from_dict(json.loads(row["envelope_json"])),
            first_receipt_sequence=int(row["first_sequence"]),
            last_receipt_sequence=int(row["last_sequence"]),
            receipt_count=int(row["receipt_count"]),
            duplicate_receipt_count=int(row["duplicate_count"] or 0),
            is_conflict=bool(row["is_conflict"]),
            acknowledged=bool(row["acknowledged"]),
        )

    @staticmethod
    def _bounded_query_sql(*, where_sql: str, order_column: str, direction: str) -> str:
        return f"""
            WITH bounded_evidence AS MATERIALIZED (
                SELECT e.evidence_id,
                       e.event_timestamp,
                       e.first_receipt_sequence
                FROM evidence_versions AS e
                {where_sql}
                ORDER BY {order_column} {direction}, e.evidence_id {direction}
                LIMIT ?
            ),
            receipt_totals AS MATERIALIZED (
                SELECT r.evidence_id,
                       MIN(r.sequence) AS first_sequence,
                       MAX(r.sequence) AS last_sequence,
                       COUNT(r.sequence) AS receipt_count,
                       SUM(CASE WHEN r.disposition = 'duplicate' THEN 1 ELSE 0 END) AS duplicate_count
                FROM bounded_evidence AS bounded
                CROSS JOIN evidence_receipts AS r INDEXED BY idx_receipts_evidence
                WHERE r.evidence_id = bounded.evidence_id
                GROUP BY r.evidence_id
            )
            SELECT e.*,
                   totals.first_sequence,
                   totals.last_sequence,
                   totals.receipt_count,
                   totals.duplicate_count,
                   EXISTS(
                       SELECT 1 FROM evidence_acknowledgements a
                       WHERE a.evidence_id = e.evidence_id AND a.consumer = ?
                   ) AS acknowledged
            FROM bounded_evidence AS bounded
            JOIN evidence_versions AS e ON e.evidence_id = bounded.evidence_id
            JOIN receipt_totals AS totals ON totals.evidence_id = bounded.evidence_id
            ORDER BY {order_column} {direction}, e.evidence_id {direction}
        """

    def query(
        self,
        *,
        source_type: str | None = None,
        source_system: str | None = None,
        assertion: str | None = None,
        event_type: str | None = None,
        dimension: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        client_session_id: str | None = None,
        work_id: str | None = None,
        section_id: str | None = None,
        idempotency_key: str | None = None,
        event_at_or_after: str | None = None,
        event_before: str | None = None,
        conflicts_only: bool = False,
        consumer: str | None = None,
        acknowledged: bool | None = None,
        order_by: str = "event_time",
        descending: bool = False,
        arrival_before_sequence: int | None = None,
        limit: int = 1000,
    ) -> list[EvidenceRecord]:
        if order_by not in _ORDER_COLUMNS:
            raise ValueError("order_by must be 'event_time' or 'arrival'")
        if arrival_before_sequence is not None:
            if (
                isinstance(arrival_before_sequence, bool)
                or not isinstance(arrival_before_sequence, int)
                or arrival_before_sequence < 0
            ):
                raise ValueError("arrival_before_sequence must be a non-negative integer")
            if order_by != "arrival":
                raise ValueError("arrival_before_sequence requires order_by='arrival'")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("e.source_type", source_type),
            ("e.source_system", source_system),
            ("e.assertion", assertion),
            ("e.event_type", event_type),
            ("e.project_id", project_id),
            ("e.run_id", run_id),
            ("e.client_session_id", client_session_id),
            ("e.work_id", work_id),
            ("e.section_id", section_id),
            ("e.idempotency_key", idempotency_key),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if dimension is not None:
            where.append("EXISTS (SELECT 1 FROM evidence_dimensions d WHERE d.evidence_id = e.evidence_id AND d.dimension = ?)")
            params.append(dimension)
        if event_at_or_after is not None:
            where.append("e.event_timestamp >= ?")
            params.append(normalize_timestamp(event_at_or_after, "event_at_or_after"))
        if event_before is not None:
            where.append("e.event_timestamp < ?")
            params.append(normalize_timestamp(event_before, "event_before"))
        if arrival_before_sequence is not None:
            where.append("e.first_receipt_sequence < ?")
            params.append(arrival_before_sequence)
        if conflicts_only:
            where.append("e.is_conflict = 1")
        consumer_value = consumer or ""
        if acknowledged is not None:
            if consumer is None:
                raise ValueError("acknowledged filtering requires consumer")
            predicate = "EXISTS" if acknowledged else "NOT EXISTS"
            where.append(
                f"{predicate} (SELECT 1 FROM evidence_acknowledgements a WHERE a.evidence_id = e.evidence_id AND a.consumer = ?)"
            )
            params.append(consumer)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        direction = "DESC" if descending else "ASC"
        # Select the requested evidence-version window before touching receipt
        # history or the large envelope_json column.  The old query grouped
        # every matching receipt and sorted complete evidence rows before
        # applying LIMIT, so even ``limit=100`` scaled with the entire store.
        # All filters and the exact public ordering remain in this first CTE;
        # receipt totals are still exact for every returned version.
        order_column = _ORDER_COLUMNS[order_by]
        query = self._bounded_query_sql(
            where_sql=where_sql,
            order_column=order_column,
            direction=direction,
        )
        with self._connection() as connection:
            rows = connection.execute(query, (*params, limit, consumer_value)).fetchall()
        return [self._record_from_row(row) for row in rows]

    @staticmethod
    def _recent_source_query(
        *,
        source_type: str,
        source_system: str | None,
        assertion: str | None,
        event_type: str | None,
        consumer: str,
        limit: int,
    ) -> tuple[str, tuple[Any, ...]]:
        """Build the bounded arrival query used by ``query_recent_source``.

        Keep the LIMIT inside the materialized ``recent_evidence`` CTE.  The
        receipts table can be much larger than the evidence-version table due
        to retries and duplicate adapter delivery; aggregating receipts before
        selecting the requested versions would make a dashboard read scale
        with the whole history instead of the requested window.
        """

        where = ["e.source_type = ?"]
        params: list[Any] = [source_type]
        for column, value in (
            ("e.source_system", source_system),
            ("e.assertion", assertion),
            ("e.event_type", event_type),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        where_sql = " AND ".join(where)
        sql = f"""
            WITH recent_evidence AS MATERIALIZED (
                SELECT e.evidence_id, e.first_receipt_sequence
                FROM evidence_versions AS e INDEXED BY idx_evidence_source_arrival
                WHERE {where_sql}
                ORDER BY e.first_receipt_sequence DESC, e.evidence_id DESC
                LIMIT ?
            ),
            receipt_totals AS MATERIALIZED (
                SELECT r.evidence_id,
                       MIN(r.sequence) AS first_sequence,
                       MAX(r.sequence) AS last_sequence,
                       COUNT(r.sequence) AS receipt_count,
                       SUM(CASE WHEN r.disposition = 'duplicate' THEN 1 ELSE 0 END) AS duplicate_count
                FROM recent_evidence AS recent
                CROSS JOIN evidence_receipts AS r INDEXED BY idx_receipts_evidence
                WHERE r.evidence_id = recent.evidence_id
                GROUP BY r.evidence_id
            )
            SELECT e.*,
                   totals.first_sequence,
                   totals.last_sequence,
                   totals.receipt_count,
                   totals.duplicate_count,
                   EXISTS(
                       SELECT 1 FROM evidence_acknowledgements a
                       WHERE a.evidence_id = e.evidence_id AND a.consumer = ?
                   ) AS acknowledged
            FROM recent_evidence AS recent
            JOIN evidence_versions AS e ON e.evidence_id = recent.evidence_id
            JOIN receipt_totals AS totals ON totals.evidence_id = recent.evidence_id
            ORDER BY recent.first_receipt_sequence DESC, recent.evidence_id DESC
        """
        return sql, (*params, limit, consumer)

    def query_recent_source(
        self,
        *,
        source_type: str,
        source_system: str | None = None,
        assertion: str | None = None,
        event_type: str | None = None,
        consumer: str | None = None,
        limit: int = 10_000,
    ) -> list[EvidenceRecord]:
        """Return the newest first-arriving versions for one source type.

        Unlike the general-purpose ``query``, this product-read path selects a
        bounded arrival window before joining and aggregating receipt history.
        Results are always ordered by first receipt, newest first.  Receipt
        counts remain exact for every returned version.
        """

        if not isinstance(source_type, str) or not source_type:
            raise ValueError("source_type is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        sql, params = self._recent_source_query(
            source_type=source_type,
            source_system=source_system,
            assertion=assertion,
            event_type=event_type,
            consumer=consumer or "",
            limit=limit,
        )
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._record_from_row(row) for row in rows]

    def replay(
        self,
        *,
        consumer: str = "default",
        after_sequence: int = 0,
        limit: int = 1000,
        include_acknowledged: bool = False,
    ) -> list[EvidenceRecord]:
        """Return arrival-ordered versions for a consumer to process/ack.

        ``after_sequence`` applies to the latest receipt, so a later duplicate
        can intentionally make an existing version visible again.  The
        projection still returns one record with its full receipt count.
        """

        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if not consumer:
            raise ValueError("consumer is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        ack_predicate = "" if include_acknowledged else "AND NOT EXISTS (SELECT 1 FROM evidence_acknowledgements a WHERE a.evidence_id = e.evidence_id AND a.consumer = ?)"
        ack_params: tuple[Any, ...] = () if include_acknowledged else (consumer,)
        sql = f"""
            SELECT e.*,
                   MIN(r.sequence) AS first_sequence,
                   MAX(r.sequence) AS last_sequence,
                   COUNT(r.sequence) AS receipt_count,
                   SUM(CASE WHEN r.disposition = 'duplicate' THEN 1 ELSE 0 END) AS duplicate_count,
                   EXISTS(
                       SELECT 1 FROM evidence_acknowledgements a
                       WHERE a.evidence_id = e.evidence_id AND a.consumer = ?
                   ) AS acknowledged
            FROM evidence_versions e
            JOIN evidence_receipts r ON r.evidence_id = e.evidence_id
            WHERE EXISTS (
                SELECT 1 FROM evidence_receipts newer
                WHERE newer.evidence_id = e.evidence_id AND newer.sequence > ?
            )
            {ack_predicate}
            GROUP BY e.evidence_id
            ORDER BY MIN(r.sequence), e.evidence_id
            LIMIT ?
        """
        with self._connection() as connection:
            rows = connection.execute(sql, (consumer, after_sequence, *ack_params, limit)).fetchall()
        return [self._record_from_row(row) for row in rows]

    pending = replay

    def acknowledge(
        self,
        evidence_id: str,
        *,
        consumer: str = "default",
        acknowledged_at: str | int | float | datetime | None = None,
    ) -> bool:
        if not consumer or "\n" in consumer or len(consumer) > 255:
            raise ValueError("consumer must be a non-empty single-line identifier")
        timestamp = normalize_timestamp(acknowledged_at, "acknowledged_at") if acknowledged_at is not None else _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute("SELECT 1 FROM evidence_versions WHERE evidence_id = ?", (evidence_id,)).fetchone()
                if exists is None:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "INSERT OR IGNORE INTO evidence_acknowledgements(consumer, evidence_id, acknowledged_at) VALUES (?, ?, ?)",
                    (consumer, evidence_id, timestamp),
                )
                connection.execute("COMMIT")
                return True
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    ack = acknowledge

    def acknowledge_many(self, evidence_ids: Sequence[str], *, consumer: str = "default") -> int:
        return sum(int(self.acknowledge(evidence_id, consumer=consumer)) for evidence_id in evidence_ids)

    def receipts(self, evidence_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT sequence, receipt_id, spool_offset, disposition, received_at
                FROM evidence_receipts WHERE evidence_id = ? ORDER BY sequence
                """,
                (evidence_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def conflicts(self, *, idempotency_key: str | None = None) -> list[EvidenceRecord]:
        return self.query(idempotency_key=idempotency_key, conflicts_only=True, order_by="arrival")

    def query_claimed_links(
        self,
        *,
        claimed_evidence_id: str | None = None,
        observed_evidence_id: str | None = None,
        validation_state: str | None = None,
        conflicts_only: bool = False,
        limit: int = 1000,
    ) -> list[ClaimedLinkRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("l.claimed_evidence_id", claimed_evidence_id),
            ("l.observed_evidence_id", observed_evidence_id),
            ("l.validation_state", validation_state),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if conflicts_only:
            where.append("l.is_conflict = 1")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT l.*, MIN(r.sequence) AS first_sequence,
                       MAX(r.sequence) AS last_sequence,
                       COUNT(r.sequence) AS receipt_count,
                       SUM(CASE WHEN r.disposition = 'duplicate' THEN 1 ELSE 0 END) AS duplicate_count
                FROM claimed_link_versions l
                JOIN claimed_link_receipts r ON r.link_id = l.link_id
                {where_sql}
                GROUP BY l.link_id
                ORDER BY MIN(r.sequence), l.link_id
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [
            ClaimedLinkRecord(
                link=ClaimedLink.from_dict(json.loads(row["link_json"])),
                first_receipt_sequence=int(row["first_sequence"]),
                last_receipt_sequence=int(row["last_sequence"]),
                receipt_count=int(row["receipt_count"]),
                duplicate_receipt_count=int(row["duplicate_count"] or 0),
                is_conflict=bool(row["is_conflict"]),
                validation_state=str(row["validation_state"]),
            )
            for row in rows
        ]

    def refreshable_usage_heads(
        self,
        *,
        include_tombstoned: bool = True,
    ) -> list[RefreshableUsageHead]:
        """Return deterministic current-state heads without mutating the store."""

        if not isinstance(include_tombstoned, bool):
            raise TypeError("include_tombstoned must be a bool")
        where = "" if include_tombstoned else "WHERE tombstoned = 0"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM refreshable_usage_heads {where} ORDER BY slot_key"
            ).fetchall()
        return [
            RefreshableUsageHead(
                slot_key=str(row["slot_key"]),
                slot_identity=json.loads(row["slot_identity_json"]),
                content_hash=str(row["content_hash"]),
                source_order=int(row["source_order"]) if row["source_order"] is not None else None,
                current_revision_id=(
                    str(row["current_revision_id"]) if row["current_revision_id"] is not None else None
                ),
                last_revision_id=str(row["last_revision_id"]),
                evidence_id=str(row["evidence_id"]),
                tombstoned=bool(row["tombstoned"]),
                updated_receipt_id=str(row["updated_receipt_id"]),
            )
            for row in rows
        ]

    def refreshable_usage_stats(self) -> RefreshableUsageStats:
        """Return physical current-state projection counts for acceptance checks."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM refreshable_usage_heads) AS heads,
                    (SELECT COUNT(*) FROM refreshable_usage_heads WHERE tombstoned = 0) AS current_heads,
                    (SELECT COUNT(*) FROM refreshable_usage_heads WHERE tombstoned = 1) AS tombstoned_heads,
                    (SELECT COUNT(*) FROM refreshable_usage_revisions) AS revisions,
                    (SELECT COUNT(*) FROM refreshable_usage_revisions WHERE status = 'current') AS current_revisions,
                    (SELECT COUNT(*) FROM refreshable_usage_revisions WHERE status = 'superseded') AS superseded_revisions,
                    (SELECT COUNT(*) FROM refreshable_usage_conflicts) AS conflicts,
                    (SELECT COUNT(*) FROM refreshable_usage_batch_receipts) AS batch_receipts,
                    (SELECT COUNT(*) FROM refreshable_usage_transitions) AS transitions
                """
            ).fetchone()
        assert row is not None
        return RefreshableUsageStats(
            heads=int(row["heads"]),
            current_heads=int(row["current_heads"]),
            tombstoned_heads=int(row["tombstoned_heads"]),
            revisions=int(row["revisions"]),
            current_revisions=int(row["current_revisions"]),
            superseded_revisions=int(row["superseded_revisions"]),
            conflicts=int(row["conflicts"]),
            batch_receipts=int(row["batch_receipts"]),
            transitions=int(row["transitions"]),
            spool_bytes=(
                self.refreshable_usage_spool_path.stat().st_size
                if self.refreshable_usage_spool_path.is_file()
                else 0
            ),
        )

    def stats(self) -> EvidenceStoreStats:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(DISTINCT idempotency_key) FROM evidence_versions) AS logical_events,
                    (SELECT COUNT(*) FROM evidence_versions) AS evidence_versions,
                    (SELECT COUNT(*) FROM evidence_receipts) AS receipts,
                    (SELECT COUNT(*) FROM evidence_receipts WHERE disposition = 'duplicate') AS duplicate_receipts,
                    (SELECT COUNT(DISTINCT idempotency_key) FROM evidence_versions WHERE is_conflict = 1) AS conflict_groups,
                    (SELECT COUNT(*) FROM evidence_versions WHERE is_conflict = 1) AS conflict_versions,
                    (SELECT COUNT(*) FROM evidence_acknowledgements) AS acknowledgements,
                    (SELECT COUNT(*) FROM claimed_link_versions) AS claimed_link_versions,
                    (SELECT COUNT(*) FROM claimed_link_receipts) AS claimed_link_receipts,
                    (SELECT COUNT(*) FROM spool_errors) AS invalid_spool_records
                """
            ).fetchone()
        assert row is not None
        return EvidenceStoreStats(
            logical_events=int(row["logical_events"]),
            evidence_versions=int(row["evidence_versions"]),
            receipts=int(row["receipts"]),
            duplicate_receipts=int(row["duplicate_receipts"]),
            conflict_groups=int(row["conflict_groups"]),
            conflict_versions=int(row["conflict_versions"]),
            acknowledgements=int(row["acknowledgements"]),
            claimed_link_versions=int(row["claimed_link_versions"]),
            claimed_link_receipts=int(row["claimed_link_receipts"]),
            invalid_spool_records=int(row["invalid_spool_records"]),
            spool_bytes=self.spool_path.stat().st_size if self.spool_path.is_file() else 0,
        )


AppendResult = EvidenceAppendResult


__all__ = [
    "EVIDENCE_PROJECTION_FILENAME",
    "EVIDENCE_SPOOL_FILENAME",
    "EVIDENCE_SPOOL_SCHEMA_VERSION",
    "EVIDENCE_STORE_DIRNAME",
    "EVIDENCE_STORE_SCHEMA_VERSION",
    "REFRESHABLE_USAGE_SPOOL_FILENAME",
    "REFRESHABLE_USAGE_SPOOL_SCHEMA_VERSION",
    "AppendResult",
    "ClaimedLinkRecord",
    "EvidenceAppendResult",
    "EvidenceRecord",
    "EvidenceStore",
    "EvidenceStoreStats",
    "LinkAppendResult",
    "RefreshableUsageHead",
    "RefreshableUsageItem",
    "RefreshableUsageReconcileResult",
    "RefreshableUsageStats",
    "SpoolReplayResult",
]
