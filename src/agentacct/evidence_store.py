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
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterator, Mapping, Sequence

from .evidence import (
    EVIDENCE_SCHEMA_VERSION,
    ClaimedLink,
    EvidenceEnvelope,
    canonical_digest,
    canonical_json_bytes,
    normalize_timestamp,
)
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

# Prune (reclaim / bound the projection). The default target is the ~99% bloat:
# the shadow copies of every recorded tool_activity — never read by any
# correctness-critical lane (independently_checked reads only source_type
# client_hook; refreshable-usage rows are local_client_log and FK-referenced).
_PRUNE_DENYLISTED_SOURCE_TYPES = frozenset({"client_hook", "local_client_log"})
_PRUNE_DEFAULT_SOURCE_TYPES = ("mcp_agent_reported",)
_PRUNE_DEFAULT_EVENT_TYPES = ("tool_activity_observed",)
_AUTO_PRUNE_METADATA_KEY = "last_auto_prune_at"

# Spool compaction (cold storage). The append-only spool is never trimmed by
# prune, so the bytes of a pruned shadow row stay on disk forever even though
# replay only moves forward and no query can reach them again. Dropping them is
# only safe under the same guards prune uses, plus a blocking verification that
# rebuilds the projection from the candidate spool before the swap.
_COMPACTION_GENERATION_KEY = "spool_compaction_generation"
_COMPACTION_TIMESTAMP_KEY = "last_compacted_at"
_COMPACTION_ARCHIVE_DIRNAME = "archive"
_COMPACTION_ARCHIVE_LEVEL = 12
_COMPACTION_COPY_CHUNK = 4 * 1024 * 1024
_COMPACTION_VERIFY_PREFIX = "agentacct-spool-compaction-verify-"
_COMPACTION_DROP = "drop"
_COMPACTION_KEEP = "keep"
_COMPACTION_UNCLASSIFIED = "unclassified"

# Every compared count of the blocking verification. Each entry is one
# ``SELECT COUNT(*)``; the names are the verification's report keys.
_COMPACTION_COUNT_QUERIES: tuple[tuple[str, str], ...] = (
    ("evidence_versions", "SELECT COUNT(*) FROM evidence_versions"),
    ("evidence_versions_conflict", "SELECT COUNT(*) FROM evidence_versions WHERE is_conflict = 1"),
    ("evidence_dimensions", "SELECT COUNT(*) FROM evidence_dimensions"),
    ("evidence_acknowledgements", "SELECT COUNT(*) FROM evidence_acknowledgements"),
    ("evidence_receipts", "SELECT COUNT(*) FROM evidence_receipts"),
    (
        "evidence_receipts_inserted",
        "SELECT COUNT(*) FROM evidence_receipts WHERE disposition = 'inserted'",
    ),
    (
        "evidence_receipts_duplicate",
        "SELECT COUNT(*) FROM evidence_receipts WHERE disposition = 'duplicate'",
    ),
    (
        "evidence_receipts_conflict",
        "SELECT COUNT(*) FROM evidence_receipts WHERE disposition = 'conflict'",
    ),
    ("claimed_link_versions", "SELECT COUNT(*) FROM claimed_link_versions"),
    ("claimed_link_versions_conflict", "SELECT COUNT(*) FROM claimed_link_versions WHERE is_conflict = 1"),
    (
        "claimed_link_receipts_inserted",
        "SELECT COUNT(*) FROM claimed_link_receipts WHERE disposition = 'inserted'",
    ),
    (
        "claimed_link_receipts_duplicate",
        "SELECT COUNT(*) FROM claimed_link_receipts WHERE disposition = 'duplicate'",
    ),
    ("refreshable_usage_batch_receipts", "SELECT COUNT(*) FROM refreshable_usage_batch_receipts"),
    ("refreshable_usage_revisions", "SELECT COUNT(*) FROM refreshable_usage_revisions"),
    (
        "refreshable_usage_revisions_current",
        "SELECT COUNT(*) FROM refreshable_usage_revisions WHERE status = 'current'",
    ),
    ("refreshable_usage_heads", "SELECT COUNT(*) FROM refreshable_usage_heads"),
    (
        "refreshable_usage_heads_tombstoned",
        "SELECT COUNT(*) FROM refreshable_usage_heads WHERE tombstoned = 1",
    ),
    ("refreshable_usage_conflicts", "SELECT COUNT(*) FROM refreshable_usage_conflicts"),
    ("refreshable_usage_transitions", "SELECT COUNT(*) FROM refreshable_usage_transitions"),
    ("spool_errors", "SELECT COUNT(*) FROM spool_errors"),
)

# Every table a reader can reach, with the columns that make one of its rows the
# same fact. The blocking verification requires each live row to appear in the
# rebuild of the compacted spool with these values unchanged, which is what keeps
# the compaction from dropping, re-keying, or rewriting anything still reachable
# by a query. Columns that a rebuild legitimately recomputes differ between the
# two projections — a receipt's `sequence` and `spool_offset`, a version's
# `first_receipt_sequence`, an error's `detected_at` — and are deliberately not
# selected here.
_COMPACTION_COVERAGE_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "evidence_versions",
        "SELECT evidence_id, idempotency_key, integrity_hash, is_conflict FROM evidence_versions",
    ),
    (
        "evidence_receipts",
        "SELECT receipt_id, evidence_id, idempotency_key, disposition FROM evidence_receipts",
    ),
    ("evidence_dimensions", "SELECT evidence_id, dimension FROM evidence_dimensions"),
    (
        "evidence_acknowledgements",
        "SELECT consumer, evidence_id, acknowledged_at FROM evidence_acknowledgements",
    ),
    (
        # `validation_state` is deliberately absent: it is derived from whether
        # the referenced evidence still exists, which prune changes and the
        # compaction does not.
        "claimed_link_versions",
        "SELECT link_id, idempotency_key, integrity_hash, claimed_evidence_id, observed_evidence_id, "
        "is_conflict FROM claimed_link_versions",
    ),
    (
        "claimed_link_receipts",
        "SELECT receipt_id, link_id, idempotency_key, disposition FROM claimed_link_receipts",
    ),
    (
        "refreshable_usage_batch_receipts",
        "SELECT receipt_id, complete, transition_count, inserted_count, updated_count, "
        "resurrected_count, watermarked_count, tombstoned_count, conflict_count "
        "FROM refreshable_usage_batch_receipts",
    ),
    (
        "refreshable_usage_revisions",
        "SELECT revision_id, slot_key, slot_identity_json, content_hash, source_order, evidence_id, "
        "status, created_receipt_id, created_transition_id, superseded_receipt_id, "
        "superseded_transition_id FROM refreshable_usage_revisions",
    ),
    (
        "refreshable_usage_heads",
        "SELECT slot_key, slot_identity_json, content_hash, source_order, current_revision_id, "
        "last_revision_id, evidence_id, tombstoned, updated_receipt_id, updated_transition_id "
        "FROM refreshable_usage_heads",
    ),
    (
        "refreshable_usage_conflicts",
        "SELECT conflict_key, slot_key, slot_identity_json, current_revision_id, "
        "current_content_hash, current_source_order, head_tombstoned, candidate_content_hash, "
        "candidate_revision_id, candidate_source_order, candidate_evidence_id, "
        "candidate_integrity_hash, first_receipt_id, first_transition_id "
        "FROM refreshable_usage_conflicts",
    ),
    (
        "refreshable_usage_transitions",
        "SELECT transition_id, receipt_id, sequence_in_batch, slot_key, action, revision_id, "
        "conflict_key, prior_revision_id, evidence_id FROM refreshable_usage_transitions",
    ),
    ("spool_errors", "SELECT raw_digest, error FROM spool_errors"),
)

# Which columns of each coverage query carry the evidence id and idempotency key
# a kept spool row can answer for. A live row none of whose accountable columns
# matches a kept row is not this spool's to lose — the refreshable-usage lane
# stores its own records in its own file — so the containment check does not
# require it. Tables without an entry are checked in full, because the
# compaction cannot touch them at all.
_COMPACTION_ACCOUNTABLE_COLUMNS: dict[str, tuple[int, ...]] = {
    "evidence_versions": (0, 1),
    "evidence_receipts": (1, 2),
    "evidence_dimensions": (0,),
    "evidence_acknowledgements": (1,),
}


@dataclass(frozen=True)
class EvidenceSnapshotState:
    """Versions for receipt capture and append-tolerant serving.

    Bind these to the projection's physical st_dev/st_ino as well: restoring
    an old backup can restore its database_id and generations too. The SQLite
    schema_cookie additionally detects backup restoration into the same inode.
    """

    database_id: str
    revision: int
    destructive_revision: int
    schema_version: str
    schema_cookie: int
    mechanical_revision: int
    mechanical_destructive_revision: int


def read_evidence_snapshot_state(projection_path: Path) -> EvidenceSnapshotState:
    """Read generations without recovery, writes, or content-row scans.

    Replay offsets and append counters in store_metadata are intentionally
    excluded. The schema value is part of the returned safety identity.
    Missing or incompatible state fails closed; serving must not reuse a
    cached token after a read error.
    """

    uri = projection_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.05)
    try:
        row = connection.execute(
            "SELECT s.database_id, s.revision, s.destructive_revision, m.value, p.schema_version, "
            "h.revision, h.destructive_revision "
            "FROM evidence_snapshot_state AS s "
            "JOIN evidence_mechanical_snapshot_state AS h ON h.singleton = s.singleton "
            "JOIN store_metadata AS m ON m.key = 'schema_version' "
            "CROSS JOIN pragma_schema_version AS p "
            "WHERE s.singleton = 1"
        ).fetchone()
    finally:
        connection.close()
    if (
        row is None
        or not isinstance(row[0], str)
        or len(row[0]) != 32
        or any(character not in "0123456789abcdef" for character in row[0])
        or not isinstance(row[1], int)
        or not isinstance(row[2], int)
        or row[1] < 0
        or row[2] < 0
        or row[3] != EVIDENCE_STORE_SCHEMA_VERSION
        or not isinstance(row[4], int)
        or not isinstance(row[5], int)
        or not isinstance(row[6], int)
        or row[5] < 0
        or row[6] < 0
    ):
        raise sqlite3.DatabaseError("evidence snapshot state is missing or invalid")
    return EvidenceSnapshotState(*row)


# Each lookup uses an existing unique index. REPLACE can silently delete via
# any unique key (including the implicit rowid) with recursive_triggers OFF.
# These guards deliberately fail closed for an ignored conflicting INSERT too.
_SNAPSHOT_UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "evidence_versions": ("evidence_id = NEW.evidence_id",),
    "evidence_dimensions": ("evidence_id = NEW.evidence_id AND dimension = NEW.dimension",),
    "evidence_receipts": ("sequence = NEW.sequence", "receipt_id = NEW.receipt_id"),
    "evidence_acknowledgements": ("consumer = NEW.consumer AND evidence_id = NEW.evidence_id",),
    "claimed_link_versions": ("link_id = NEW.link_id",),
    "claimed_link_receipts": ("sequence = NEW.sequence", "receipt_id = NEW.receipt_id"),
    "refreshable_usage_batch_receipts": ("receipt_id = NEW.receipt_id",),
    "refreshable_usage_revisions": (
        "revision_id = NEW.revision_id",
        "created_transition_id = NEW.created_transition_id",
        "slot_key = NEW.slot_key AND status = 'current' AND NEW.status = 'current'",
    ),
    "refreshable_usage_heads": ("slot_key = NEW.slot_key",),
    "refreshable_usage_conflicts": ("conflict_key = NEW.conflict_key", "first_transition_id = NEW.first_transition_id"),
    "refreshable_usage_transitions": (
        "transition_id = NEW.transition_id",
        "receipt_id = NEW.receipt_id AND sequence_in_batch = NEW.sequence_in_batch",
    ),
    "spool_errors": ("spool_offset = NEW.spool_offset AND raw_digest = NEW.raw_digest",),
}


def _initialize_snapshot_state(connection: sqlite3.Connection) -> None:
    """Install durable guards once; legacy writers automatically run them."""

    connection.executescript("""
        CREATE TABLE IF NOT EXISTS evidence_snapshot_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            database_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            destructive_revision INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO evidence_snapshot_state
            (singleton, database_id, revision, destructive_revision)
        VALUES (1, lower(hex(randomblob(16))), 0, 0);
        CREATE TRIGGER IF NOT EXISTS evidence_schema_snapshot_update
        AFTER UPDATE ON store_metadata
        WHEN (OLD.key = 'schema_version' OR NEW.key = 'schema_version')
            AND (OLD.key IS NOT NEW.key OR OLD.value IS NOT NEW.value)
        BEGIN
            UPDATE evidence_snapshot_state
            SET revision = revision + 1, destructive_revision = destructive_revision + 1
            WHERE singleton = 1;
        END;
        CREATE TRIGGER IF NOT EXISTS evidence_schema_snapshot_delete
        AFTER DELETE ON store_metadata WHEN OLD.key = 'schema_version'
        BEGIN
            UPDATE evidence_snapshot_state
            SET revision = revision + 1, destructive_revision = destructive_revision + 1
            WHERE singleton = 1;
        END;
        CREATE TRIGGER IF NOT EXISTS evidence_schema_snapshot_replace
        BEFORE INSERT ON store_metadata
        WHEN NEW.key = 'schema_version'
            AND EXISTS (SELECT 1 FROM store_metadata WHERE key = NEW.key AND value IS NOT NEW.value)
        BEGIN
            UPDATE evidence_snapshot_state
            SET revision = revision + 1, destructive_revision = destructive_revision + 1
            WHERE singleton = 1;
        END;
    """)
    for table, keys in _SNAPSHOT_UNIQUE_KEYS.items():
        columns = ["rowid", *(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))]
        changed = [f'NEW."{column}" IS NOT OLD."{column}"' for column in columns]
        destructive = [
            f'(OLD.first_receipt_sequence IS NOT NULL AND {condition})'
            if column == "first_receipt_sequence" else condition
            for column, condition in zip(columns, changed)
        ]
        # Separate EXISTS clauses retain each unique index, including the
        # partial current-slot index; no per-write projection scan is needed.
        collision = " OR ".join(
            f'EXISTS (SELECT 1 FROM "{table}" WHERE {key})'
            for key in ("rowid = NEW.rowid", *keys)
        )
        connection.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_snapshot_insert
            AFTER INSERT ON "{table}"
            BEGIN
                UPDATE evidence_snapshot_state SET revision = revision + 1 WHERE singleton = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_snapshot_update
            AFTER UPDATE ON "{table}" WHEN {' OR '.join(changed)}
            BEGIN
                UPDATE evidence_snapshot_state
                SET revision = revision + 1,
                    destructive_revision = destructive_revision + CASE WHEN {' OR '.join(destructive)} THEN 1 ELSE 0 END
                WHERE singleton = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_snapshot_delete
            AFTER DELETE ON "{table}"
            BEGIN
                UPDATE evidence_snapshot_state
                SET revision = revision + 1, destructive_revision = destructive_revision + 1
                WHERE singleton = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_snapshot_replace
            BEFORE INSERT ON "{table}" WHEN {collision}
            BEGIN
                UPDATE evidence_snapshot_state
                SET revision = revision + 1, destructive_revision = destructive_revision + 1
                WHERE singleton = 1;
            END;
        """)
    _initialize_mechanical_snapshot_state(connection)


def _initialize_mechanical_snapshot_state(connection: sqlite3.Connection) -> None:
    """Version the hook window and its conflict groups, not unrelated usage.

    Receipt capture reads versions + receipt availability for client_hook and
    expands their full idempotency groups. It does not read usage heads, links,
    acknowledgements or dimension-index rows. Numeric usage refreshes in those
    other lanes must not invalidate already served mechanical evidence.
    """

    connection.executescript("""
        CREATE TABLE IF NOT EXISTS evidence_mechanical_snapshot_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            revision INTEGER NOT NULL,
            destructive_revision INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO evidence_mechanical_snapshot_state VALUES (1, 0, 0);
        CREATE INDEX IF NOT EXISTS idx_evidence_mechanical_group
            ON evidence_versions(idempotency_key, source_type, evidence_id);
    """)

    def version_member(alias: str) -> str:
        return (
            f"({alias}.source_type = 'client_hook' OR EXISTS ("
            "SELECT 1 FROM evidence_versions AS hook INDEXED BY idx_evidence_mechanical_group "
            f"WHERE hook.idempotency_key = {alias}.idempotency_key AND hook.source_type = 'client_hook'))"
        )

    def receipt_member(alias: str) -> str:
        return (
            "EXISTS (SELECT 1 FROM evidence_versions AS member "
            f"WHERE member.evidence_id = {alias}.evidence_id AND {version_member('member')})"
        )

    for table in ("evidence_versions", "evidence_receipts"):
        member = version_member if table == "evidence_versions" else receipt_member
        columns = ["rowid", *(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))]
        changes = [f'NEW."{column}" IS NOT OLD."{column}"' for column in columns]
        destructive = [
            f'(OLD.first_receipt_sequence IS NOT NULL AND {condition})'
            if column == "first_receipt_sequence" else condition
            for column, condition in zip(columns, changes)
        ]
        collisions = " OR ".join(
            f'EXISTS (SELECT 1 FROM "{table}" AS prior WHERE {key} AND {member("prior")})'
            for key in ("prior.rowid = NEW.rowid", *(
                " AND ".join("prior." + part for part in key.split(" AND "))
                for key in _SNAPSHOT_UNIQUE_KEYS[table]
            ))
        )
        connection.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_mechanical_insert
            AFTER INSERT ON "{table}" WHEN {member('NEW')}
            BEGIN
                UPDATE evidence_mechanical_snapshot_state SET revision = revision + 1 WHERE singleton = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_mechanical_update
            BEFORE UPDATE ON "{table}"
            WHEN ({' OR '.join(changes)}) AND ({member('OLD')} OR {member('NEW')} OR {collisions})
            BEGIN
                UPDATE evidence_mechanical_snapshot_state
                SET revision = revision + 1,
                    destructive_revision = destructive_revision + CASE WHEN {' OR '.join(destructive)} THEN 1 ELSE 0 END
                WHERE singleton = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_mechanical_delete
            BEFORE DELETE ON "{table}" WHEN {member('OLD')}
            BEGIN
                UPDATE evidence_mechanical_snapshot_state
                SET revision = revision + 1, destructive_revision = destructive_revision + 1 WHERE singleton = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_mechanical_replace
            BEFORE INSERT ON "{table}" WHEN {collisions}
            BEGIN
                UPDATE evidence_mechanical_snapshot_state
                SET revision = revision + 1, destructive_revision = destructive_revision + 1 WHERE singleton = 1;
            END;
        """)
    connection.executescript("""
        -- A newly visible member can retract a previously accepted conflict
        -- group, even when an old writer does not update its is_conflict flag.
        CREATE TRIGGER IF NOT EXISTS evidence_versions_mechanical_group_insert
        BEFORE INSERT ON evidence_versions
        WHEN EXISTS (
            SELECT 1 FROM evidence_versions AS hook INDEXED BY idx_evidence_mechanical_group
            WHERE hook.idempotency_key = NEW.idempotency_key AND hook.source_type = 'client_hook'
                AND hook.evidence_id != NEW.evidence_id
        )
        BEGIN
            UPDATE evidence_mechanical_snapshot_state
            SET revision = revision + 1, destructive_revision = destructive_revision + 1 WHERE singleton = 1;
        END;
        -- Handle legacy writers that commit a version before its first
        -- receipt: the receipt JOIN makes it visible only at this later write.
        CREATE TRIGGER IF NOT EXISTS evidence_receipts_mechanical_group_insert
        BEFORE INSERT ON evidence_receipts
        WHEN NOT EXISTS (SELECT 1 FROM evidence_receipts WHERE evidence_id = NEW.evidence_id)
            AND EXISTS (
                SELECT 1 FROM evidence_versions AS member WHERE member.evidence_id = NEW.evidence_id
                    AND EXISTS (
                        SELECT 1 FROM evidence_versions AS hook INDEXED BY idx_evidence_mechanical_group
                        WHERE hook.idempotency_key = member.idempotency_key AND hook.source_type = 'client_hook'
                            AND hook.evidence_id != member.evidence_id
                    )
            )
        BEGIN
            UPDATE evidence_mechanical_snapshot_state
            SET revision = revision + 1, destructive_revision = destructive_revision + 1 WHERE singleton = 1;
        END;
    """)


@dataclass(frozen=True)
class EvidencePruneResult:
    dry_run: bool
    matched_versions: int
    deleted_versions: int
    deleted_receipts: int
    deleted_dimensions: int
    deleted_acknowledgements: int
    batches: int
    bytes_before: int
    bytes_after: int
    vacuumed: bool

    def bytes_reclaimed(self) -> int:
        return max(0, self.bytes_before - self.bytes_after)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "matched_versions": self.matched_versions,
            "deleted_versions": self.deleted_versions,
            "deleted_receipts": self.deleted_receipts,
            "deleted_dimensions": self.deleted_dimensions,
            "deleted_acknowledgements": self.deleted_acknowledgements,
            "batches": self.batches,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "bytes_reclaimed": self.bytes_reclaimed(),
            "vacuumed": self.vacuumed,
        }


@dataclass(frozen=True)
class EvidenceSpoolCompactionResult:
    """Outcome of ``EvidenceStore.compact_spool``.

    ``kept_rows`` counts the snapshot rows that survived the drop rule, while
    ``rows_after`` is the final live spool row count: a concurrent writer that
    appends between the snapshot and the swap adds rows, and those are copied
    verbatim rather than filtered. ``dropped_bytes`` is the exact byte count
    removed, ``archive_bytes`` is the verified archive size (0 when no archive
    was written), and ``verification`` carries the blocking comparison that
    gated the swap: the per-count ``baseline``/``rebuilt`` values plus the
    equality flags, where both sides are from-zero rebuilds of the same snapshot
    prefix — the spool as it is, and the compacted candidate. A dry run never
    rebuilds either, so its ``verification`` reports ``outcome="dry_run"`` and
    no comparison at all.
    """

    dry_run: bool
    spool_bytes_before: int
    spool_bytes_after: int
    rows_before: int
    rows_after: int
    dropped_rows: int
    kept_rows: int
    dropped_bytes: int
    archived_path: str | None
    archive_bytes: int | None
    swapped: bool
    generation: int
    verification: dict[str, Any]
    warnings: tuple[str, ...]

    def bytes_reclaimed(self) -> int:
        return max(0, self.spool_bytes_before - self.spool_bytes_after)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "spool_bytes_before": self.spool_bytes_before,
            "spool_bytes_after": self.spool_bytes_after,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "dropped_rows": self.dropped_rows,
            "kept_rows": self.kept_rows,
            "dropped_bytes": self.dropped_bytes,
            "archived_path": self.archived_path,
            "archive_bytes": self.archive_bytes,
            "swapped": self.swapped,
            "generation": self.generation,
            "verification": dict(self.verification),
            "warnings": list(self.warnings),
            "bytes_reclaimed": self.bytes_reclaimed(),
        }


@dataclass(frozen=True)
class _SpoolCompactionPlan:
    """What a filtered candidate spool holds, before anything is swapped."""

    rows_before: int
    rows_kept: int
    rows_dropped: int
    unclassified_rows: int
    dropped_bytes: int
    candidate_main_bytes: int
    candidate_refreshable_bytes: int
    refreshable_rows: int
    fences_remapped: int
    kept_identities: frozenset[str]


@dataclass(frozen=True)
class _SpoolCompactionPreparation:
    """The locked read of a compaction: snapshots, guards, and the drop rule.

    ``main_identity``/``refreshable_identity`` are the ``(st_dev, st_ino)`` of
    the spools the snapshots were linked from. They are re-checked under the
    retaken lock, because the unlocked phase must not swap a candidate onto a
    spool file that another actor replaced meanwhile.
    """

    dry_run: bool
    generation: int
    spool_bytes_before: int
    refreshable_bytes_before: int
    surviving_keys: frozenset[str]
    guarded_ids: frozenset[str]
    main_source: Path
    refreshable_source: Path
    main_identity: tuple[int, int] | None
    refreshable_identity: tuple[int, int] | None
    snapshot_main: Path | None
    snapshot_refreshable: Path | None
    candidate_main: Path | None
    candidate_refreshable: Path | None


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

    def __init__(self, root: Path | str, *, durable: bool = True) -> None:
        """Open (or create) the store rooted at ``root``.

        ``durable=False`` is for throwaway projections only: ``compact_spool``
        rebuilds a candidate projection in a scratch directory purely to compare
        it and then discards it, and the per-row fsync a live store needs costs
        orders of magnitude more than the rebuild itself on a multi-million row
        spool. Losing a scratch projection loses nothing.
        """

        if root is None:
            raise ValueError("evidence store root is required")
        self.root = Path(root).expanduser()
        self.evidence_root = self.root / EVIDENCE_STORE_DIRNAME
        self.spool_path = self.evidence_root / EVIDENCE_SPOOL_FILENAME
        self.refreshable_usage_spool_path = self.evidence_root / REFRESHABLE_USAGE_SPOOL_FILENAME
        self.projection_path = self.evidence_root / EVIDENCE_PROJECTION_FILENAME
        self.lock_path = self.evidence_root / ".spool.lock"
        self._durable = bool(durable)
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
        connection.execute("PRAGMA synchronous = " + ("FULL" if self._durable else "OFF"))
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_projection(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = " + ("FULL" if self._durable else "OFF"))
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
            _initialize_snapshot_state(connection)

    def snapshot_state(self) -> EvidenceSnapshotState:
        return read_evidence_snapshot_state(self.projection_path)

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

    def prune_versions(
        self,
        *,
        source_types: Sequence[str] | None = None,
        event_types: Sequence[str] | None = None,
        older_than: str | None = None,
        batch_size: int = 5000,
        max_rows: int | None = None,
        dry_run: bool = True,
        vacuum: bool = False,
    ) -> EvidencePruneResult:
        """Delete non-consumed shadow evidence versions (default: the
        mcp_agent_reported/tool_activity_observed bloat) plus their receipts,
        acknowledgements and dimensions transactionally, and optionally VACUUM
        to return the freed pages to the OS.

        The append-only ``spool.jsonl`` is NEVER touched. Durability holds
        because ``recover()`` only replays FORWARD from a cursor that sits at
        spool EOF, so pruned projection rows are not resurrected on reopen. Three
        guards keep the honesty-critical lanes intact: a hard denylist on
        client_hook / local_client_log, exclusion subqueries that skip any
        evidence_id referenced by the refreshable-usage or claimed-link tables,
        and PRAGMA foreign_keys=ON as a final net.
        """

        if not (100 <= batch_size <= 100_000):
            raise ValueError("batch_size must be between 100 and 100000")
        stypes = tuple(source_types) if source_types else _PRUNE_DEFAULT_SOURCE_TYPES
        etypes = tuple(event_types) if event_types else _PRUNE_DEFAULT_EVENT_TYPES
        if not stypes or not etypes:
            raise ValueError("refusing to prune: an empty type selection would match everything")
        denied = _PRUNE_DENYLISTED_SOURCE_TYPES.intersection(stypes)
        if denied:
            raise ValueError(f"refusing to prune honesty-critical source types: {sorted(denied)}")

        def _empty(dry: bool, matched: int, before: int) -> EvidencePruneResult:
            return EvidencePruneResult(
                dry_run=dry,
                matched_versions=matched,
                deleted_versions=0,
                deleted_receipts=0,
                deleted_dimensions=0,
                deleted_acknowledgements=0,
                batches=0,
                bytes_before=before,
                bytes_after=before,
                vacuumed=False,
            )

        with self._locked():
            bytes_before = self.projection_path.stat().st_size if self.projection_path.is_file() else 0
            if not self.projection_path.is_file():
                return _empty(dry_run, 0, bytes_before)

            source_ph = ",".join("?" for _ in stypes)
            event_ph = ",".join("?" for _ in etypes)
            where = f"source_type IN ({source_ph}) AND event_type IN ({event_ph})"
            params: list[Any] = [*stypes, *etypes]
            if older_than:
                where += " AND event_timestamp < ?"
                params.append(older_than)

            # Referenced ids belong to the refreshable-usage / claimed-link lanes;
            # they are never the tool_activity default target but the subqueries
            # make a broadened --source-type/--event-type run safe too.
            select_sql = (
                "CREATE TEMP TABLE _prune_targets AS\n"
                "SELECT evidence_id FROM evidence_versions\n"
                f"WHERE {where}\n"
                "  AND evidence_id NOT IN (SELECT evidence_id FROM refreshable_usage_revisions)\n"
                "  AND evidence_id NOT IN (SELECT evidence_id FROM refreshable_usage_heads)\n"
                "  AND evidence_id NOT IN (SELECT evidence_id FROM refreshable_usage_transitions WHERE evidence_id IS NOT NULL)\n"
                "  AND evidence_id NOT IN (SELECT candidate_evidence_id FROM refreshable_usage_conflicts)\n"
                "  AND evidence_id NOT IN (SELECT claimed_evidence_id FROM claimed_link_versions)\n"
                "  AND evidence_id NOT IN (SELECT observed_evidence_id FROM claimed_link_versions)"
            )

            deleted_versions = deleted_receipts = deleted_dims = deleted_acks = batches = 0
            # Keep the IN (...) placeholder count well under SQLite's bound-variable
            # limit (32766 since 3.32) even if a caller passes a huge batch_size.
            chunk = min(batch_size, 20_000)
            with self._connection() as connection:
                # Tune this connection for a bulk delete over a projection far
                # larger than the default 2MB cache. foreign_keys=OFF is SAFE
                # here: we delete each target's receipts/acks/dimensions BEFORE
                # its version, and _prune_targets already excludes every id
                # referenced by the refreshable-usage / claimed-link lanes, so no
                # orphan can be created — while ON forces ~one index probe per
                # referencing table per deleted row, all cache-missing to disk on
                # a multi-GB store (the dominant cost). NORMAL sync + a large
                # cache/mmap turn scattered random I/O into far fewer disk seeks.
                # All safe for a re-runnable, per-batch-committed operation whose
                # source of truth is the untouched append-only spool.
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute("PRAGMA cache_size = -1048576")  # ~1 GiB page cache
                connection.execute("PRAGMA mmap_size = 1073741824")  # 1 GiB mmap
                connection.execute("PRAGMA temp_store = MEMORY")
                connection.execute("DROP TABLE IF EXISTS _prune_targets")
                connection.execute(select_sql, params)
                matched = int(connection.execute("SELECT COUNT(*) FROM _prune_targets").fetchone()[0])
                if dry_run or matched == 0:
                    connection.execute("DROP TABLE IF EXISTS _prune_targets")
                    return _empty(dry_run, matched, bytes_before)

                # Walk the target set FORWARD by rowid (the temp table's implicit
                # integer key) instead of deleting from it each batch. A
                # `DELETE FROM _prune_targets WHERE evidence_id IN (...)` would
                # full-scan the unindexed temp table every iteration — O(N^2)
                # over millions of rows. The forward walk is one O(N) pass; the
                # per-batch main-table deletes use the evidence_id indexes.
                last_rowid = 0
                while True:
                    if max_rows is not None and deleted_versions >= max_rows:
                        break
                    rows = connection.execute(
                        "SELECT rowid, evidence_id FROM _prune_targets WHERE rowid > ? ORDER BY rowid LIMIT ?",
                        (last_rowid, chunk),
                    ).fetchall()
                    if not rows:
                        break
                    last_rowid = rows[-1][0]
                    ids = [row[1] for row in rows]
                    placeholders = ",".join("?" for _ in ids)
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        deleted_receipts += connection.execute(
                            f"DELETE FROM evidence_receipts WHERE evidence_id IN ({placeholders})", ids
                        ).rowcount
                        deleted_acks += connection.execute(
                            f"DELETE FROM evidence_acknowledgements WHERE evidence_id IN ({placeholders})", ids
                        ).rowcount
                        deleted_dims += connection.execute(
                            f"DELETE FROM evidence_dimensions WHERE evidence_id IN ({placeholders})", ids
                        ).rowcount
                        deleted_versions += connection.execute(
                            f"DELETE FROM evidence_versions WHERE evidence_id IN ({placeholders})", ids
                        ).rowcount
                        connection.execute("COMMIT")
                    except Exception:
                        connection.execute("ROLLBACK")
                        raise
                    batches += 1
                connection.execute("DROP TABLE IF EXISTS _prune_targets")

            vacuumed = False
            if vacuum:
                with self._connection() as connection:
                    connection.execute("VACUUM")
                _owner_only(self.projection_path, 0o600)
                vacuumed = True

            bytes_after = self.projection_path.stat().st_size if self.projection_path.is_file() else bytes_before
            return EvidencePruneResult(
                dry_run=False,
                matched_versions=matched,
                deleted_versions=deleted_versions,
                deleted_receipts=deleted_receipts,
                deleted_dimensions=deleted_dims,
                deleted_acknowledgements=deleted_acks,
                batches=batches,
                bytes_before=bytes_before,
                bytes_after=bytes_after,
                vacuumed=vacuumed,
            )

    def auto_prune_if_due(
        self,
        *,
        min_interval_seconds: float,
        older_than_seconds: float,
        max_rows: int | None,
        now: float | None = None,
    ) -> EvidencePruneResult | None:
        """Throttled, non-VACUUM prune for the managed watcher loop.

        Freed pages are reused (no VACUUM), so the projection stops growing
        unboundedly without the expensive whole-file rewrite. Returns None when
        the last run was within ``min_interval_seconds``.
        """

        now = time.time() if now is None else float(now)
        last = self._stored_replay_offset(_AUTO_PRUNE_METADATA_KEY)
        if last and (now - last) < min_interval_seconds:
            return None
        cutoff = None
        if older_than_seconds and older_than_seconds > 0:
            cutoff = normalize_timestamp(now - older_than_seconds)
        result = self.prune_versions(
            older_than=cutoff,
            batch_size=5000,
            max_rows=max_rows,
            dry_run=False,
            vacuum=False,
        )
        self._set_stored_replay_offset(_AUTO_PRUNE_METADATA_KEY, int(now))
        return result

    def compact_spool(
        self,
        *,
        dry_run: bool = True,
        archive: bool = True,
        now: float | None = None,
    ) -> EvidenceSpoolCompactionResult:
        """Drop the spool rows the projection can no longer reach.

        ``prune_versions`` trims the projection and never the append-only
        ``spool.jsonl``, so a pruned shadow row's bytes stay on disk forever
        even though ``recover`` only ever replays FORWARD from the EOF cursor
        and no query can reach them again. This rewrites the spool without
        exactly those rows:

        * Droppable = the default prune target (``mcp_agent_reported`` /
          ``tool_activity_observed``) whose ``idempotency_key`` no longer
          appears in ``evidence_versions``, whose ``evidence_id`` is not
          referenced by the refreshable-usage or claimed-link lanes (the
          exclusion subqueries ``prune_versions`` uses), and whose record still
          validates. ``client_hook`` and ``local_client_log`` are never
          dropped, and every other row is rewritten byte for byte, in order,
          so duplicate receipts stay visible exactly as before.
        * The refreshable-usage spool is rewritten with re-mapped
          ``main_spool_fence`` values and re-derived ``record_hash`` digests,
          so a from-zero replay still interleaves both spools in arrival order.
        * The swap only happens after a blocking verification rebuilds the
          projection from *both* spools in a scratch directory and finds every
          compared count, the whole arrival order, and the spool-error count
          identical: the baseline rebuild is the spool as it is, the rebuilt one
          is the candidate, and both are from-zero replays of the same snapshot
          prefix. The live projection is deliberately not the baseline (prune
          deletes versions and receipts the append-only spool still holds, so a
          rebuild that resurrects one of them can never equal it again), and the
          identities the compaction drops are removed from both rebuilds before
          the counts are compared. A mismatch, an archive failure, or a replay
          failure aborts with the original spool untouched.
        * A single ``os.link`` snapshot keeps the original bytes alive — never a
          second copy of the spool. With ``archive`` the snapshot is compressed
          to ``<store dir>/archive/spool-<UTC date>-gen<generation>.jsonl.zst``
          (zstandard level 12) and read back before the snapshot link is
          released.

        Only ``dry_run=False`` writes. A dry run scans, classifies, and measures
        the spool — no candidate file, no rebuild, no archive — because two
        from-zero replays of a multi-gigabyte spool are the heaviest part of the
        write path; its ``verification`` therefore reports ``outcome="dry_run"``
        and the blocking comparison runs only with ``--write``. Either way every
        file in the store is left exactly as it was found. A run that finds
        nothing droppable rewrites nothing.

        Receipt ``spool_offset`` values in the projection keep their
        pre-compaction coordinates: they are arrival metadata for the row as it
        was received, no read path resolves them against the spool, and
        rewriting them would bump the projection's destructive revision and
        invalidate served snapshots for a metadata-only change.
        """

        moment = time.time() if now is None else float(now)
        preparation: _SpoolCompactionPreparation | None = None
        try:
            # The snapshot and the drop rule's inputs are read under the lock;
            # the scan, both verification rebuilds, and the archive then run
            # unlocked so a live watcher keeps appending, and the lock is
            # retaken only to append those receipts and to exchange the files.
            with self._locked():
                preparation = self._prepare_spool_compaction(dry_run=dry_run)
            return self._run_spool_compaction(preparation, archive=archive, now=moment)
        finally:
            if preparation is not None:
                self._discard_spool_compaction_files(preparation)

    def _prepare_spool_compaction(self, *, dry_run: bool) -> _SpoolCompactionPreparation:
        """Read everything that only holds still while no writer can move it."""

        generation = self._compaction_generation() + 1
        spool_bytes_before = self._path_size(self.spool_path)
        refreshable_bytes_before = self._path_size(self.refreshable_usage_spool_path)
        if not dry_run:
            # Project every durable receipt before the drop rule reads the
            # projection, so no durable row is judged unreachable merely because
            # a crash left it unprojected.
            self._recover_unlocked()
        surviving_keys, guarded_ids = self._compaction_protected_identities()

        token = uuid.uuid4().hex[:16]
        if dry_run:
            # A dry run rewrites nothing at all: no snapshot, no candidate, no
            # rebuild. It reads the live spool and the projection and reports
            # what a real run would drop.
            return _SpoolCompactionPreparation(
                dry_run=True,
                generation=generation,
                spool_bytes_before=spool_bytes_before,
                refreshable_bytes_before=refreshable_bytes_before,
                surviving_keys=surviving_keys,
                guarded_ids=guarded_ids,
                main_source=self.spool_path,
                refreshable_source=self.refreshable_usage_spool_path,
                main_identity=None,
                refreshable_identity=None,
                snapshot_main=None,
                snapshot_refreshable=None,
                candidate_main=None,
                candidate_refreshable=None,
            )

        snapshot_main = self.evidence_root / f".spool-compaction-{token}.source.jsonl"
        snapshot_refreshable = self.evidence_root / f".spool-compaction-{token}.source-refreshable.jsonl"
        candidate_main = self.evidence_root / f".spool-compaction-{token}.jsonl"
        candidate_refreshable = self.evidence_root / f".spool-compaction-{token}.refreshable.jsonl"
        main_identity = self._spool_identity(self.spool_path)
        refreshable_identity = self._spool_identity(self.refreshable_usage_spool_path)
        if spool_bytes_before:
            os.link(self.spool_path, snapshot_main)
        else:
            snapshot_main = None
        if refreshable_bytes_before:
            os.link(self.refreshable_usage_spool_path, snapshot_refreshable)
        else:
            snapshot_refreshable = None
        return _SpoolCompactionPreparation(
            dry_run=False,
            generation=generation,
            spool_bytes_before=spool_bytes_before,
            refreshable_bytes_before=refreshable_bytes_before,
            surviving_keys=surviving_keys,
            guarded_ids=guarded_ids,
            main_source=snapshot_main if snapshot_main is not None else self.spool_path,
            refreshable_source=(
                snapshot_refreshable
                if snapshot_refreshable is not None
                else self.refreshable_usage_spool_path
            ),
            main_identity=main_identity,
            refreshable_identity=refreshable_identity,
            snapshot_main=snapshot_main,
            snapshot_refreshable=snapshot_refreshable,
            candidate_main=candidate_main,
            candidate_refreshable=candidate_refreshable,
        )

    def _run_spool_compaction(
        self,
        preparation: _SpoolCompactionPreparation,
        *,
        archive: bool,
        now: float,
    ) -> EvidenceSpoolCompactionResult:
        dry_run = preparation.dry_run
        generation = preparation.generation
        spool_bytes_before = preparation.spool_bytes_before
        refreshable_bytes_before = preparation.refreshable_bytes_before
        main_limit = preparation.spool_bytes_before
        refreshable_limit = preparation.refreshable_bytes_before
        candidate_main = preparation.candidate_main
        candidate_refreshable = preparation.candidate_refreshable
        warnings: list[str] = []
        archived_path: Path | None = None
        archive_bytes = 0
        verification: dict[str, Any] = {}
        plan = self._filter_spool_snapshot(
            main_source=preparation.main_source,
            main_limit=main_limit,
            refreshable_source=preparation.refreshable_source,
            refreshable_limit=refreshable_limit,
            candidate_main=candidate_main,
            candidate_refreshable=candidate_refreshable,
            surviving_keys=preparation.surviving_keys,
            guarded_ids=preparation.guarded_ids,
            warnings=warnings,
        )
        if plan.unclassified_rows:
            warnings.append(
                f"{plan.unclassified_rows} spool record(s) could not be classified as the default "
                "prune target; they are retained verbatim rather than dropped"
            )
        if plan.rows_dropped == 0:
            return self._compaction_result(
                dry_run=dry_run,
                plan=plan,
                spool_bytes_before=spool_bytes_before,
                spool_bytes_after=spool_bytes_before,
                rows_after=plan.rows_before,
                archived_path=None,
                archive_bytes=0,
                swapped=False,
                generation=generation,
                verification=self._compaction_nothing_to_do(),
                warnings=warnings,
            )

        if dry_run:
            # The blocking verification rebuilds a projection, which is the
            # heaviest part of a compaction, and a dry run exists to be cheap: it
            # scans, classifies, and measures, and says so in its own report.
            # Nothing was written, so there is nothing to verify either.
            return self._compaction_result(
                dry_run=True,
                plan=plan,
                spool_bytes_before=spool_bytes_before,
                spool_bytes_after=plan.candidate_main_bytes,
                rows_after=plan.rows_kept,
                archived_path=None,
                archive_bytes=0,
                swapped=False,
                generation=generation,
                verification={
                    "outcome": "dry_run",
                    "equivalent": None,
                    "reason": (
                        "a dry run scans, classifies, and measures only; the blocking verification "
                        "rebuilds the candidate projection and runs with --write"
                    ),
                    "candidate_rows": plan.rows_kept,
                    "candidate_bytes": plan.candidate_main_bytes,
                },
                warnings=warnings,
            )

        verification = self._verify_compaction_candidate(
            plan=plan,
            candidate_main=candidate_main,
            candidate_refreshable=candidate_refreshable,
            main_limit=main_limit,
            refreshable_limit=refreshable_limit,
            warnings=warnings,
        )
        verification["candidate_rows"] = plan.rows_kept
        verification["candidate_bytes"] = plan.candidate_main_bytes
        verification["refreshable_records"] = plan.refreshable_rows
        verification["refreshable_fences_remapped"] = plan.fences_remapped
        if verification.get("equivalent") is not True:
            verification["outcome"] = "aborted"
            verification["abort_reason"] = (
                "live rows were missing from the rebuild of the compacted spool; "
                "the spool was left untouched"
            )
            warnings.append(
                "blocking verification failed, so nothing was swapped: "
                f"{sorted(verification.get('mismatches', {}))}"
            )
            return self._compaction_result(
                dry_run=dry_run,
                plan=plan,
                spool_bytes_before=spool_bytes_before,
                spool_bytes_after=spool_bytes_before,
                rows_after=plan.rows_before,
                archived_path=None,
                archive_bytes=0,
                swapped=False,
                generation=generation,
                verification=verification,
                warnings=warnings,
            )

        if archive:
            try:
                archived_path, archive_bytes = self._archive_compaction_snapshot(
                    snapshot=(
                        preparation.snapshot_main
                        if preparation.snapshot_main is not None
                        else preparation.main_source
                    ),
                    generation=generation,
                    now=now,
                )
            except Exception as exc:  # archive failures must abort the swap
                verification["outcome"] = "aborted"
                verification["abort_reason"] = f"the cold archive failed: {type(exc).__name__}: {exc}"
                warnings.append(
                    "the cold archive could not be written and read back, so nothing was swapped: "
                    f"{type(exc).__name__}: {exc}"
                )
                return self._compaction_result(
                    dry_run=dry_run,
                    plan=plan,
                    spool_bytes_before=spool_bytes_before,
                    spool_bytes_after=spool_bytes_before,
                    rows_after=plan.rows_before,
                    archived_path=None,
                    archive_bytes=0,
                    swapped=False,
                    generation=generation,
                    verification=verification,
                    warnings=warnings,
                )
            if preparation.snapshot_main is not None:
                # The verified archive is the copy of record for the exact
                # pre-compaction bytes; release the hard link now. (The cleanup
                # pass unlinks again, which is a no-op.)
                preparation.snapshot_main.unlink(missing_ok=True)

        with self._locked():
            if not self._compaction_swap_is_safe(preparation):
                verification["outcome"] = "aborted"
                verification["abort_reason"] = (
                    "the live spools were replaced or truncated while the candidate was built; "
                    "the spool was left untouched"
                )
                warnings.append(
                    "the live spools changed identity while the candidate was built, so nothing was swapped"
                )
                self._discard_compaction_archive(archived_path, warnings)
                return self._compaction_result(
                    dry_run=False,
                    plan=plan,
                    spool_bytes_before=spool_bytes_before,
                    spool_bytes_after=spool_bytes_before,
                    rows_after=plan.rows_before,
                    archived_path=None,
                    archive_bytes=0,
                    swapped=False,
                    generation=generation,
                    verification=verification,
                    warnings=warnings,
                )
            swapped, new_main_size, _ = self._apply_compaction_swap(
                plan=plan,
                candidate_main=candidate_main,
                candidate_refreshable=candidate_refreshable,
                snapshot_main_size=main_limit,
                snapshot_refreshable_size=refreshable_limit,
                generation=generation,
                now=now,
                warnings=warnings,
            )
        if not swapped:
            verification["outcome"] = "aborted"
            verification["abort_reason"] = (
                "the spool changed while the candidate was built; the live spool was left untouched"
            )
            self._discard_compaction_archive(archived_path, warnings)
            return self._compaction_result(
                dry_run=False,
                plan=plan,
                spool_bytes_before=spool_bytes_before,
                spool_bytes_after=spool_bytes_before,
                rows_after=plan.rows_before,
                archived_path=None,
                archive_bytes=0,
                swapped=False,
                generation=generation,
                verification=verification,
                warnings=warnings,
            )
        verification["outcome"] = "swapped"
        if archived_path is None:
            warnings.append(
                "no archive was written, so the dropped rows exist only as the compacted spool's absence"
            )
        return self._compaction_result(
            dry_run=False,
            plan=plan,
            spool_bytes_before=spool_bytes_before,
            spool_bytes_after=new_main_size,
            rows_after=self._count_spool_rows(self.spool_path),
            archived_path=archived_path,
            archive_bytes=archive_bytes,
            swapped=True,
            generation=generation,
            verification=verification,
            warnings=warnings,
        )

    @staticmethod
    def _spool_identity(path: Path) -> tuple[int, int] | None:
        try:
            info = path.stat()
        except OSError:
            return None
        return (info.st_dev, info.st_ino)

    @staticmethod
    def _discard_compaction_archive(archived_path: Path | None, warnings: list[str]) -> None:
        """Drop an archive whose swap never happened.

        The archive exists to keep rows the compaction removed. When the swap
        aborts the spool still holds them, so the file is redundant — and
        leaving it would make the next run collide with a generation that was
        never recorded.
        """

        if archived_path is None:
            return
        try:
            archived_path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - cleanup is best effort
            warnings.append(f"the archive for an aborted swap could not be removed: {exc}")
            return
        warnings.append(
            f"the swap was aborted, so the archive of the unchanged spool was removed: {archived_path}"
        )

    def _compaction_swap_is_safe(self, preparation: _SpoolCompactionPreparation) -> bool:
        """The snapshots must still describe the live spools to swap at all.

        A matching device/inode pair proves the live path still names the very
        inode that was snapshotted, so the bytes before the snapshot boundary
        are untouched (the store only ever appends) and the post-snapshot bytes
        really are this snapshot's extension. Any other store replacing or
        truncating a spool fails this check and nothing is swapped.
        """

        if self._spool_identity(self.spool_path) != preparation.main_identity:
            return False
        if self._spool_identity(self.refreshable_usage_spool_path) != preparation.refreshable_identity:
            return False
        if self._path_size(self.spool_path) < preparation.spool_bytes_before:
            return False
        return self._path_size(self.refreshable_usage_spool_path) >= preparation.refreshable_bytes_before

    def _discard_spool_compaction_files(self, preparation: _SpoolCompactionPreparation) -> None:
        """Remove every temporary the compaction created, swapped or not."""

        for temporary in (
            preparation.candidate_main,
            preparation.candidate_refreshable,
            preparation.snapshot_main,
            preparation.snapshot_refreshable,
        ):
            if temporary is None:
                continue
            try:
                temporary.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - cleanup is best effort
                pass

    def _compaction_result(
        self,
        *,
        dry_run: bool,
        plan: _SpoolCompactionPlan,
        spool_bytes_before: int,
        spool_bytes_after: int,
        rows_after: int,
        archived_path: Path | None,
        archive_bytes: int,
        swapped: bool,
        generation: int,
        verification: dict[str, Any],
        warnings: list[str],
    ) -> EvidenceSpoolCompactionResult:
        return EvidenceSpoolCompactionResult(
            dry_run=dry_run,
            spool_bytes_before=spool_bytes_before,
            spool_bytes_after=spool_bytes_after,
            rows_before=plan.rows_before,
            rows_after=rows_after,
            dropped_rows=plan.rows_dropped,
            kept_rows=plan.rows_kept,
            dropped_bytes=plan.dropped_bytes,
            archived_path=str(archived_path) if archived_path is not None else None,
            archive_bytes=archive_bytes,
            swapped=swapped,
            generation=generation,
            verification=verification,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _count_spool_rows(path: Path) -> int:
        rows = 0
        if not path.is_file():
            return 0
        with path.open("rb") as handle:
            while True:
                raw = handle.readline()
                if not raw:
                    break
                rows += 1
        return rows

    @staticmethod
    def _path_size(path: Path | None) -> int:
        if path is None:
            return 0
        try:
            return path.stat().st_size if path.is_file() else 0
        except OSError:
            return 0

    def _compaction_generation(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM store_metadata WHERE key = ?",
                (_COMPACTION_GENERATION_KEY,),
            ).fetchone()
        if row is None:
            return 0
        try:
            return max(0, int(row["value"]))
        except (TypeError, ValueError):
            return 0

    def _set_store_metadata(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO store_metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _compaction_protected_identities(self) -> tuple[frozenset[str], frozenset[str]]:
        """Live idempotency keys plus the evidence ids prune never touches.

        A row may only be dropped when its idempotency key answers to nothing in
        the projection; the id set reuses ``prune_versions``' exclusion
        subqueries verbatim so a row referenced by the refreshable-usage or
        claimed-link lanes is never dropped, even if its own version row were
        ever missing.
        """

        guarded: set[str] = set()
        with self._connection() as connection:
            surviving_keys = frozenset(
                str(row[0])
                for row in connection.execute("SELECT DISTINCT idempotency_key FROM evidence_versions")
            )
            for sql in (
                "SELECT evidence_id FROM refreshable_usage_revisions",
                "SELECT evidence_id FROM refreshable_usage_heads",
                "SELECT evidence_id FROM refreshable_usage_transitions WHERE evidence_id IS NOT NULL",
                "SELECT candidate_evidence_id FROM refreshable_usage_conflicts",
                "SELECT claimed_evidence_id FROM claimed_link_versions",
                "SELECT observed_evidence_id FROM claimed_link_versions",
            ):
                guarded.update(str(row[0]) for row in connection.execute(sql))
        return surviving_keys, frozenset(guarded)

    def _projection_compaction_summary(self) -> dict[str, Any]:
        """The compared projection state: counts, arrival order, spool errors.

        The arrival digest walks ``evidence_versions`` in exactly the order
        ``query(order_by="arrival")`` returns (``first_receipt_sequence`` then
        ``evidence_id``) without the public method's 10k row cap, so the
        comparison stays constant-memory on a multi-million row projection.
        """

        counts: dict[str, int] = {}
        digest = hashlib.sha256()
        arrival_rows = 0
        with self._connection() as connection:
            for name, sql in _COMPACTION_COUNT_QUERIES:
                counts[name] = int(connection.execute(sql).fetchone()[0])
            cursor = connection.execute(
                "SELECT evidence_id FROM evidence_versions "
                "ORDER BY first_receipt_sequence ASC, evidence_id ASC"
            )
            while True:
                batch = cursor.fetchmany(20_000)
                if not batch:
                    break
                digest.update("\n".join(str(row[0]) for row in batch).encode("utf-8") + b"\n")
                arrival_rows += len(batch)
        return {
            "counts": counts,
            "arrival_digest": digest.hexdigest(),
            "arrival_rows": arrival_rows,
            "spool_errors": counts["spool_errors"],
        }

    @staticmethod
    def _compaction_nothing_to_do() -> dict[str, Any]:
        """Verification report for a run with no droppable rows.

        Nothing can differ: with no row dropped the spool bytes are identical,
        so the projection a rebuild would produce is the projection that is
        already there — and no rebuild has to be paid for.
        """

        return {
            "outcome": "nothing_to_do",
            "equivalent": True,
            "reason": "no spool row was droppable, so the spool bytes are unchanged",
            "mismatches": {},
        }

    def _coverage_rows(self, sql: str) -> Iterator[tuple[Any, ...]]:
        """Stream this projection's rows for one coverage query, as plain tuples."""

        with self._connection() as connection:
            cursor = connection.execute(sql)
            for row in cursor:
                yield tuple(row)

    def _spool_tail_identities(self, main_limit: int, refreshable_limit: int) -> set[str]:
        """The evidence ids and keys the spools hold *past* the snapshot.

        A writer keeps appending while the candidate is built, and those rows are
        copied to the candidate verbatim at swap time. They are therefore not
        part of the comparison — but their identities must not read as rows the
        compaction lost, which is what this collects. Any evidence id or
        idempotency key found in the tail's records counts, at whatever nesting
        the record uses: the tail is small, and a missed identity would only be a
        spurious abort.
        """

        identities: set[str] = set()
        for path, limit in (
            (self.spool_path, main_limit),
            (self.refreshable_usage_spool_path, refreshable_limit),
        ):
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                handle.seek(max(0, limit))
                while True:
                    raw = handle.readline()
                    if not raw:
                        break
                    try:
                        record = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    pending: list[Any] = [record]
                    while pending:
                        value = pending.pop()
                        if isinstance(value, Mapping):
                            for key, item in value.items():
                                if key in {"evidence_id", "idempotency_key"} and isinstance(item, str):
                                    identities.add(item)
                                elif isinstance(item, (Mapping, list, tuple)):
                                    pending.append(item)
                        elif isinstance(value, (list, tuple)):
                            pending.extend(value)
        return identities

    def _compare_rebuild_coverage(
        self,
        rebuilt: EvidenceStore,
        appended: frozenset[str],
        accountable: frozenset[str],
    ) -> tuple[dict[str, Any], int, set[str]]:
        """Every row a reader can reach has to survive the compaction.

        The comparison is a containment, not an equality, and that is the point.
        ``prune_versions`` deletes versions, receipts, dimensions, and
        acknowledgements for rows the spool still holds, so a from-zero rebuild
        of the compacted spool *legitimately* carries rows the live projection no
        longer has: comparing the two for equality reports a correct compaction
        as lossy on any store that was ever pruned, and no baseline built from
        the spool escapes that — a from-zero replay of the 22.8 GB store's 3 M
        row spool runs at about 137 rows/s (the replay opens a connection per
        row), which is roughly six hours. Containment cannot be invalidated that
        way: prune only ever removes rows, so whatever live still answers for has
        to be in the rebuild, unchanged.

        Two properties of the drop rule make containment exactly the right test:

        * a dropped row's ``idempotency_key`` is absent from the live projection
          (that *is* the drop rule), so no live version, receipt, dimension, or
          acknowledgement shares its key — removing it cannot alter a live row's
          facts, dispositions, or conflict flags;
        * every row sharing a live key is kept, so the rebuild replays each live
          key's whole group, in order, and reproduces those facts exactly.

        ``appended`` holds the identities of rows a writer added to the spool
        after the snapshot was taken. Those are copied to the candidate verbatim
        at swap time and are no part of this comparison, so they must not read as
        rows the compaction lost.

        ``accountable`` holds the evidence ids and idempotency keys the kept
        *main-spool* rows carry. A live row no kept row answers for was never
        this spool's to lose — the refreshable-usage lane stores its own records
        elsewhere, and a store can carry rows projected by an older code path
        that the current spool cannot reproduce — so only accountable rows are
        required to appear in the rebuild. That keeps the check exact where it
        can be exact: anything this spool carried is still required, with its
        facts unchanged.

        Returns the mismatches, how many live rows were checked, and the live
        evidence ids the arrival-order digest is restricted to.
        """

        mismatches: dict[str, Any] = {}
        checked = 0
        live_ids: set[str] = set()
        for name, sql in _COMPACTION_COVERAGE_QUERIES:
            rebuilt_rows = set(rebuilt._coverage_rows(sql))
            missing = 0
            sample: list[list[str]] = []
            for row in self._coverage_rows(sql):
                if any(str(value) in appended for value in row):
                    continue
                if not self._coverage_row_is_accountable(row, name, accountable):
                    continue
                checked += 1
                if name == "evidence_versions":
                    live_ids.add(str(row[0]))
                if row in rebuilt_rows:
                    continue
                missing += 1
                if len(sample) < 3:
                    sample.append([str(value) for value in row])
            if missing:
                mismatches[name] = {"live_rows_missing_from_the_rebuild": missing, "sample": sample}
        return mismatches, checked, live_ids

    @staticmethod
    def _coverage_row_is_accountable(
        row: tuple[Any, ...],
        name: str,
        accountable: frozenset[str],
    ) -> bool:
        """Whether the kept spool rows answer for this live row's identity."""

        columns = _COMPACTION_ACCOUNTABLE_COLUMNS.get(name)
        if columns is None:
            # Tables the drop rule cannot touch: every live row is required.
            return True
        return any(str(row[index]) in accountable for index in columns if index < len(row))

    def _coverage_arrival_digest(self, restrict_to: set[str] | None = None) -> tuple[str, int]:
        """Digest the arrival order of this projection's evidence versions.

        ``restrict_to`` keeps only the given evidence ids, which is how the
        rebuild's order is compared with the live one: the rebuild may carry
        extra rows (the ones prune removed), but every row live has must still
        arrive in the same relative order, or a fence re-map or a lost row moved
        it.
        """

        digest = hashlib.sha256()
        rows = 0
        with self._connection() as connection:
            cursor = connection.execute(
                "SELECT evidence_id FROM evidence_versions "
                "ORDER BY first_receipt_sequence ASC, evidence_id ASC"
            )
            while True:
                batch = cursor.fetchmany(20_000)
                if not batch:
                    break
                ids = [str(row[0]) for row in batch]
                if restrict_to is not None:
                    ids = [evidence_id for evidence_id in ids if evidence_id in restrict_to]
                if not ids:
                    continue
                digest.update("\n".join(ids).encode("utf-8") + b"\n")
                rows += len(ids)
        return digest.hexdigest(), rows

    def _rebuild_spool_projection(
        self,
        *,
        root: Path,
        main_source: Path,
        refreshable_source: Path | None,
    ) -> EvidenceStore:
        """Hard-link the given spools into a scratch store and replay them.

        The links are why a rebuild costs no extra spool bytes: the scratch store
        reads the very inode the store reads, and reads it to the end because the
        candidate is a private file nothing appends to. The scratch projection is
        opened without durability: replaying rows at a live store's per-row fsync
        costs orders of magnitude more than the replay itself, and this
        projection is discarded as soon as it has been compared.
        """

        store = EvidenceStore(root, durable=False)
        if main_source.is_file():
            os.link(main_source, store.spool_path)
        if refreshable_source is not None and refreshable_source.is_file():
            os.link(refreshable_source, store.refreshable_usage_spool_path)
        store._recover_unlocked()
        return store

    def _verify_compaction_candidate(
        self,
        *,
        plan: _SpoolCompactionPlan,
        candidate_main: Path,
        candidate_refreshable: Path | None,
        main_limit: int,
        refreshable_limit: int,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Rebuild the candidate from zero and check nothing live was lost.

        This is the blocking gate: a mismatch, a replay failure, or an unusable
        candidate leaves the original spool in place.

        The rebuild is the pre-existing cost of a compaction and it is bounded by
        the *kept* rows, not by the spool: a store kept 3 M rows to drop 60 k, so
        verifying the candidate costs a minute where replaying the whole prefix
        costs hours (measured on the 22.8 GB store: ~137 rows/s, because the
        replay opens a connection per row, so a from-zero baseline of that spool
        is about six hours). The rebuild is therefore compared by *identity* with
        the live projection rather than for equality against it — see
        `_compare_rebuild_coverage` for why containment is the right test and why
        every other form of that comparison is either unsound or unaffordable.

        What is compared, in full: every row of every table a reader can reach,
        as identities and facts; the relative arrival order of the live evidence
        ids; and the candidate's own arithmetic as the snapshot's remainder — its
        size against the snapshot's minus ``dropped_bytes``, and its row count
        against ``kept_rows``, because a kept row is copied byte for byte.
        """

        def rebuild_failure(exc: BaseException) -> dict[str, Any]:
            warnings.append(
                "blocking verification could not rebuild the candidate projection, so nothing was swapped: "
                f"{type(exc).__name__}: {exc}"
            )
            return {
                "equivalent": False,
                "mismatches": {"replay": f"{type(exc).__name__}: {exc}"},
                "counts": {},
                "live_rows_checked": 0,
                "arrival_order_rows": 0,
                "arrival_order_equal": False,
            }

        try:
            with tempfile.TemporaryDirectory(prefix=_COMPACTION_VERIFY_PREFIX) as scratch:
                rebuilt_store = self._rebuild_spool_projection(
                    root=Path(scratch) / "candidate",
                    main_source=candidate_main,
                    refreshable_source=candidate_refreshable,
                )
                appended = frozenset(
                    self._spool_tail_identities(main_limit, refreshable_limit)
                )
                live_counts = dict(self._projection_compaction_summary()["counts"])
                rebuilt_counts = dict(rebuilt_store._projection_compaction_summary()["counts"])
                mismatches, live_rows, live_ids = self._compare_rebuild_coverage(
                    rebuilt_store, appended, plan.kept_identities
                )
                live_digest, live_order_rows = self._coverage_arrival_digest(live_ids)
                rebuilt_digest, rebuilt_order_rows = rebuilt_store._coverage_arrival_digest(live_ids)
        except Exception as exc:  # noqa: BLE001 - a failed rebuild must abort, not raise
            return rebuild_failure(exc)

        arrival_equal = bool(
            live_digest == rebuilt_digest and live_order_rows == rebuilt_order_rows
        )
        if not arrival_equal:
            mismatches["arrival_order"] = {
                "live_rows": live_order_rows,
                "rebuilt_rows": rebuilt_order_rows,
            }

        # The candidate has to be the snapshot's own remainder: a kept row is
        # copied byte for byte, so its size and row count are arithmetic. That is
        # what catches a filter that silently lost or duplicated a row, which the
        # identity comparison above would read as the compaction's own doing.
        candidate_bytes = self._path_size(candidate_main)
        if candidate_bytes != plan.candidate_main_bytes:
            mismatches["candidate_bytes"] = {
                "expected": plan.candidate_main_bytes,
                "found": candidate_bytes,
            }
        candidate_rows = self._count_spool_rows(candidate_main)
        if candidate_rows != plan.rows_kept:
            mismatches["candidate_rows"] = {"expected": plan.rows_kept, "found": candidate_rows}

        verification = {
            "equivalent": not mismatches,
            "mismatches": mismatches,
            "live_rows_checked": live_rows,
            "arrival_order_rows": live_order_rows,
            "arrival_order_equal": arrival_equal,
            # Both sides, for the record. The rebuilt side normally carries more
            # rows than the live projection because it resurrects the ones prune
            # removed, which is exactly why these numbers are information and not
            # the comparison.
            "counts": {
                name: {"live": value, "rebuilt": rebuilt_counts.get(name)}
                for name, value in live_counts.items()
            },
        }
        if mismatches:
            warnings.append(
                "blocking verification found live rows missing from the rebuild, "
                f"so nothing was swapped: {sorted(mismatches)}"
            )
        return verification

    def _filter_spool_snapshot(
        self,
        *,
        main_source: Path,
        main_limit: int,
        refreshable_source: Path,
        refreshable_limit: int,
        candidate_main: Path | None,
        candidate_refreshable: Path | None,
        surviving_keys: frozenset[str],
        guarded_ids: frozenset[str],
        warnings: list[str],
    ) -> _SpoolCompactionPlan:
        """Rewrite the snapshot without the unreachable rows.

        Reads only the snapshot's first ``main_limit`` bytes and tracks the byte
        offset every kept row lands on, which is what re-maps the refreshable
        fences. The candidate file is created lazily at the first dropped row,
        so a spool with nothing to drop is never rewritten at all. A ``None``
        ``candidate_main`` makes this a pure scan: the same classification and
        the same counts with no file written and no refreshable spool read,
        which is why a dry run can classify a 20 GB spool without touching the
        disk.
        """

        refreshable_records: list[bytes] | None = None
        refreshable_fences: list[int | None] = []
        if candidate_main is not None:
            refreshable_records, refreshable_fences = self._refreshable_rewrite_plan(
                refreshable_source, refreshable_limit, warnings
            )
        fence_targets = [0] * len(refreshable_fences)
        fence_index = 0
        rows_before = rows_kept = rows_dropped = unclassified = dropped_bytes = 0
        kept_identities: set[str] = set()
        output = None
        source = (
            main_source.open("rb")
            if (main_source is not None and main_limit > 0 and main_source.is_file())
            else None
        )
        try:
            # The snapshot is a hard link, so a writer appending during this pass
            # grows the very inode being read: read exactly the prefix that
            # existed when it was taken and never a byte past it, or the copy of
            # the post-snapshot bytes would be doubled.
            buffer = b""
            consumed = 0
            offset = 0
            while source is not None:
                while b"\n" not in buffer and consumed < main_limit:
                    chunk = source.read(min(_COMPACTION_COPY_CHUNK, main_limit - consumed))
                    if not chunk:
                        break
                    buffer += chunk
                    consumed += len(chunk)
                if not buffer:
                    break
                boundary = buffer.find(b"\n")
                if boundary == -1:
                    raw, buffer = buffer, b""
                else:
                    raw, buffer = buffer[: boundary + 1], buffer[boundary + 1 :]
                record_start = offset
                offset += len(raw)
                rows_before += 1
                verdict = self._compaction_row_verdict(raw, surviving_keys, guarded_ids)
                new_offset = record_start - dropped_bytes
                # A fence names a record boundary and the kept rows keep their
                # order, so the first kept row at-or-after it becomes the fence.
                # Records without a readable fence are replayed as invalid
                # records and never claim a boundary.
                while fence_index < len(refreshable_fences):
                    declared = refreshable_fences[fence_index]
                    if declared is None:
                        fence_index += 1
                        continue
                    if declared > record_start:
                        break
                    fence_targets[fence_index] = new_offset
                    fence_index += 1
                if verdict == _COMPACTION_DROP:
                    if candidate_main is not None and output is None:
                        output = candidate_main.open("xb")
                        _owner_only(candidate_main, 0o600)
                        self._copy_spool_prefix(source, output, record_start)
                        source.seek(consumed)
                    rows_dropped += 1
                    dropped_bytes += len(raw)
                    continue
                if verdict == _COMPACTION_UNCLASSIFIED:
                    unclassified += 1
                rows_kept += 1
                if output is not None:
                    output.write(raw)
                if candidate_main is not None:
                    # The identities the kept rows carry are what a live row has
                    # to be answerable by: the drop rule only ever removes rows
                    # from this spool, so a live row no kept row answers for was
                    # never this spool's to lose.
                    evidence_id, idempotency_key = self._spool_row_identity(raw)
                    if evidence_id is not None:
                        kept_identities.add(evidence_id)
                    if idempotency_key is not None:
                        kept_identities.add(idempotency_key)
            candidate_main_bytes = max(0, offset - dropped_bytes)
            while fence_index < len(refreshable_fences):
                if refreshable_fences[fence_index] is not None:
                    fence_targets[fence_index] = candidate_main_bytes
                fence_index += 1
        finally:
            if source is not None:
                source.close()
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
                output.close()

        # With nothing dropped the fence mapping is the identity, so there is no
        # refreshable candidate to write at all: the spool stays untouched.
        candidate_refreshable_bytes = refreshable_limit
        fences_remapped = 0
        if refreshable_records is not None and candidate_refreshable is not None and rows_dropped > 0:
            with candidate_refreshable.open("xb") as target:
                _owner_only(candidate_refreshable, 0o600)
                for raw, fence, target_offset in zip(
                    refreshable_records, refreshable_fences, fence_targets
                ):
                    if fence is None or fence == target_offset:
                        target.write(raw)
                        continue
                    rewritten = self._refreshable_record_with_fence(raw, target_offset)
                    if rewritten is None:
                        target.write(raw)
                        warnings.append(
                            "a refreshable usage spool record could not be re-encoded with its new fence; "
                            "it is kept verbatim"
                        )
                        continue
                    target.write(rewritten)
                    fences_remapped += 1
                target.flush()
                os.fsync(target.fileno())
            candidate_refreshable_bytes = self._path_size(candidate_refreshable)
        return _SpoolCompactionPlan(
            rows_before=rows_before,
            rows_kept=rows_kept,
            rows_dropped=rows_dropped,
            unclassified_rows=unclassified,
            dropped_bytes=dropped_bytes,
            candidate_main_bytes=candidate_main_bytes,
            candidate_refreshable_bytes=candidate_refreshable_bytes,
            refreshable_rows=len(refreshable_records) if refreshable_records is not None else 0,
            fences_remapped=fences_remapped,
            kept_identities=frozenset(kept_identities),
        )

    @staticmethod
    def _spool_row_identity(raw: bytes) -> tuple[str | None, str | None]:
        """The evidence id and idempotency key a spool row carries, if it carries them."""

        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, None
        if not isinstance(record, Mapping) or record.get("kind") != "evidence":
            return None, None
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return None, None
        evidence_id = payload.get("evidence_id")
        idempotency_key = payload.get("idempotency_key")
        return (
            evidence_id if isinstance(evidence_id, str) else None,
            idempotency_key if isinstance(idempotency_key, str) else None,
        )

    @staticmethod
    def _copy_spool_prefix(source: IO[bytes], target: IO[bytes], length: int) -> None:
        """Copy exactly ``length`` bytes from the start of an open snapshot."""

        source.seek(0)
        remaining = length
        while remaining > 0:
            chunk = source.read(min(_COMPACTION_COPY_CHUNK, remaining))
            if not chunk:
                raise OSError("the spool snapshot ended before the first dropped row")
            target.write(chunk)
            remaining -= len(chunk)

    def _compaction_row_verdict(
        self,
        raw: bytes,
        surviving_keys: frozenset[str],
        guarded_ids: frozenset[str],
    ) -> str:
        """Classify one spool line: drop, keep, or unclassified (keep verbatim).

        Unclassified means the line cannot be shown to be the default prune
        target, so it is always kept: a malformed or unreadable record still
        replays as the invalid record it always was rather than disappearing.
        """

        if not raw.strip():
            return _COMPACTION_KEEP
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _COMPACTION_UNCLASSIFIED
        if not isinstance(record, Mapping) or record.get("kind") != "evidence":
            return _COMPACTION_KEEP
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return _COMPACTION_UNCLASSIFIED
        if payload.get("source_type") not in _PRUNE_DEFAULT_SOURCE_TYPES:
            return _COMPACTION_KEEP
        if payload.get("event_type") not in _PRUNE_DEFAULT_EVENT_TYPES:
            return _COMPACTION_KEEP
        if payload.get("source_type") in _PRUNE_DENYLISTED_SOURCE_TYPES:
            return _COMPACTION_KEEP
        if payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
            return _COMPACTION_UNCLASSIFIED
        idempotency_key = payload.get("idempotency_key")
        evidence_id = payload.get("evidence_id")
        if not isinstance(idempotency_key, str) or not isinstance(evidence_id, str):
            return _COMPACTION_UNCLASSIFIED
        if idempotency_key in surviving_keys or evidence_id in guarded_ids:
            return _COMPACTION_KEEP
        try:
            kind, _ = self._validate_spool_record(record)
        except (TypeError, ValueError, KeyError):
            return _COMPACTION_UNCLASSIFIED
        if kind != "evidence":
            return _COMPACTION_KEEP
        return _COMPACTION_DROP

    def _refreshable_rewrite_plan(
        self,
        source: Path,
        limit: int,
        warnings: list[str],
    ) -> tuple[list[bytes] | None, list[int | None]]:
        """Read the refreshable snapshot and note each record's fence."""

        if source is None or limit <= 0 or not source.is_file():
            return None, []
        records: list[bytes] = []
        fences: list[int | None] = []
        previous = 0
        unreadable = 0
        unordered = 0
        with source.open("rb") as handle:
            offset = 0
            while offset < limit:
                raw = handle.readline()
                if not raw:
                    break
                offset += len(raw)
                records.append(raw)
                try:
                    record = json.loads(raw)
                    _, fence = self._validate_refreshable_usage_spool_record(record)
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, KeyError):
                    fences.append(None)
                    unreadable += 1
                    continue
                if fence < previous:
                    unordered += 1
                previous = fence
                fences.append(fence)
        if unreadable:
            warnings.append(
                f"{unreadable} refreshable usage spool record(s) did not validate; they are kept verbatim "
                "and still replay as invalid records exactly as before"
            )
        if unordered:
            warnings.append(
                f"{unordered} refreshable usage spool record(s) carry a fence below the previous record's; "
                "fences are re-mapped in file order"
            )
        return records, fences

    @staticmethod
    def _refreshable_record_with_fence(raw: bytes, fence: int) -> bytes | None:
        """Re-serialize one refreshable record with a re-mapped fence.

        The record hash covers the fence, so it is re-derived from the same
        canonical body. Returns None when the line cannot be re-encoded.
        """

        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(record, Mapping):
            return None
        body = {key: value for key, value in record.items() if key != "record_hash"}
        body["main_spool_fence"] = int(fence)
        return canonical_json_bytes({**body, "record_hash": canonical_digest(body)}) + b"\n"

    def _apply_compaction_swap(
        self,
        *,
        plan: _SpoolCompactionPlan,
        candidate_main: Path,
        candidate_refreshable: Path | None,
        snapshot_main_size: int,
        snapshot_refreshable_size: int,
        generation: int,
        now: float,
        warnings: list[str],
    ) -> tuple[bool, int, int]:
        """Append the post-snapshot receipts, then exchange both spool files.

        Returns ``(swapped, main_bytes, refreshable_bytes)``. The hard link
        snapshot is never written to, so the bytes recovered here are the ones
        committed after the snapshot: they are copied verbatim, and their
        refreshable fences are translated by the same offset delta.
        """

        current_main_size = self._path_size(self.spool_path)
        if current_main_size < snapshot_main_size:
            warnings.append("the spool shrank while the candidate was built; refusing to swap")
            return False, 0, 0
        current_refreshable_size = self._path_size(self.refreshable_usage_spool_path)
        if current_refreshable_size < snapshot_refreshable_size:
            warnings.append(
                "the refreshable usage spool shrank while the candidate was built; refusing to swap"
            )
            return False, 0, 0
        if not candidate_main.is_file():
            warnings.append("the compacted spool candidate is missing; refusing to swap")
            return False, 0, 0

        delta_start = plan.candidate_main_bytes
        with self.spool_path.open("rb") as source, candidate_main.open("ab") as target:
            try:
                os.fchmod(target.fileno(), 0o600)
            except OSError:
                pass
            source.seek(snapshot_main_size)
            shutil.copyfileobj(source, target, _COMPACTION_COPY_CHUNK)
            target.flush()
            os.fsync(target.fileno())
        new_main_size = self._path_size(candidate_main)

        new_refreshable_size = plan.candidate_refreshable_bytes
        if current_refreshable_size > snapshot_refreshable_size:
            if candidate_refreshable is None:
                warnings.append("the refreshable usage spool grew but has no candidate; refusing to swap")
                return False, 0, 0
            new_refreshable_size = self._append_refreshable_compaction_delta(
                candidate=candidate_refreshable,
                snapshot_size=snapshot_refreshable_size,
                current_size=current_refreshable_size,
                shift=delta_start - snapshot_main_size,
                snapshot_main_size=snapshot_main_size,
                warnings=warnings,
            )

        # The refreshable file goes first: its fences already name offsets in
        # the new main spool, and a crash between the two renames can only leave
        # fences that point into a longer original file (harmless) rather than
        # past the end of a shorter one (a hard replay failure).
        if candidate_refreshable is not None and candidate_refreshable.is_file():
            os.replace(candidate_refreshable, self.refreshable_usage_spool_path)
            _owner_only(self.refreshable_usage_spool_path, 0o600)
        os.replace(candidate_main, self.spool_path)
        _owner_only(self.spool_path, 0o600)
        self._set_replay_offset(new_main_size)
        self._set_refreshable_usage_replay_offset(new_refreshable_size)
        self._set_store_metadata(_COMPACTION_GENERATION_KEY, str(generation))
        self._set_store_metadata(
            _COMPACTION_TIMESTAMP_KEY,
            datetime.fromtimestamp(now, timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        )
        self._fsync_directory(self.evidence_root)
        return True, new_main_size, new_refreshable_size

    def _append_refreshable_compaction_delta(
        self,
        *,
        candidate: Path,
        snapshot_size: int,
        current_size: int,
        shift: int,
        snapshot_main_size: int,
        warnings: list[str],
    ) -> int:
        """Copy the refreshable records written after the snapshot, re-fenced."""

        with self.refreshable_usage_spool_path.open("rb") as source, candidate.open("ab") as target:
            source.seek(snapshot_size)
            while True:
                offset = source.tell()
                if offset >= current_size:
                    break
                raw = source.readline()
                if not raw:
                    break
                if offset + len(raw) > current_size:
                    # A torn tail is not a record; its bytes are preserved.
                    target.write(raw)
                    continue
                fence: int | None = None
                try:
                    record = json.loads(raw)
                    _, fence = self._validate_refreshable_usage_spool_record(record)
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, KeyError):
                    fence = None
                if fence is None:
                    warnings.append(
                        "a refreshable usage record written during the compaction could not be re-encoded "
                        "with its new fence; it is kept verbatim"
                    )
                    target.write(raw)
                    continue
                if fence < snapshot_main_size:
                    warnings.append(
                        "a refreshable usage record written during the compaction points before the "
                        "snapshot boundary; it is kept verbatim"
                    )
                    target.write(raw)
                    continue
                new_fence = fence + shift
                if new_fence == fence:
                    target.write(raw)
                    continue
                rewritten = self._refreshable_record_with_fence(raw, new_fence)
                if rewritten is None:
                    warnings.append(
                        "a refreshable usage record written during the compaction could not be re-encoded "
                        "with its new fence; it is kept verbatim"
                    )
                    target.write(raw)
                    continue
                target.write(rewritten)
            target.flush()
            os.fsync(target.fileno())
        return self._path_size(candidate)

    def _archive_compaction_snapshot(
        self,
        *,
        snapshot: Path,
        generation: int,
        now: float,
    ) -> tuple[Path, int]:
        """Compress the pre-compaction snapshot and read it back before trusting it.

        The archive lands in ``<store dir>/archive`` — beside the store's
        ``evidence-v2`` tree rather than beside the store dir, so two stores
        under one parent cannot collide on ``spool-<date>-gen<N>`` and a store
        copy carries its archive with it. An existing target is never
        overwritten: the compression is the only copy of the dropped rows, so a
        name collision aborts instead of destroying it.

        The whole point of the archive is that the removed rows stay
        recoverable, so the compressed copy is streamed back and compared byte
        for byte and line for line. Any failure removes the partial file and
        raises, which aborts the swap upstream.
        """

        try:
            import zstandard
        except ImportError as exc:  # pragma: no cover - declared runtime dependency
            raise RuntimeError("zstandard is required to archive a compacted spool") from exc

        archive_dir = self.root / _COMPACTION_ARCHIVE_DIRNAME
        archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(archive_dir, 0o700)
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")
        target = archive_dir / f"spool-{stamp}-gen{generation}.jsonl.zst"
        if target.exists():
            raise RuntimeError(f"refusing to overwrite the existing archive at {target}")

        source_bytes = 0
        source_lines = 0
        source_digest = hashlib.sha256()
        restored_bytes = 0
        restored_lines = 0
        restored_digest = hashlib.sha256()
        try:
            # A content checksum travels inside the frame, so a later read of the
            # archive detects corruption long after this run.
            compressor = zstandard.ZstdCompressor(
                level=_COMPACTION_ARCHIVE_LEVEL,
                write_checksum=True,
            )
            with snapshot.open("rb") as handle, target.open("xb") as destination:
                _owner_only(target, 0o600)
                with compressor.stream_writer(destination, closefd=False) as writer:
                    while True:
                        chunk = handle.read(_COMPACTION_COPY_CHUNK)
                        if not chunk:
                            break
                        source_bytes += len(chunk)
                        source_lines += chunk.count(b"\n")
                        source_digest.update(chunk)
                        writer.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            with target.open("rb") as handle:
                with zstandard.ZstdDecompressor().stream_reader(handle) as reader:
                    while True:
                        chunk = reader.read(_COMPACTION_COPY_CHUNK)
                        if not chunk:
                            break
                        restored_bytes += len(chunk)
                        restored_lines += chunk.count(b"\n")
                        restored_digest.update(chunk)
            if (
                restored_bytes != source_bytes
                or restored_lines != source_lines
                or restored_digest.digest() != source_digest.digest()
            ):
                raise RuntimeError(
                    "the archived spool did not read back identically: "
                    f"{restored_bytes}/{restored_lines} vs {source_bytes}/{source_lines}"
                )
        except BaseException:
            try:
                target.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - cleanup is best effort
                pass
            raise
        self._fsync_directory(archive_dir)
        return target, self._path_size(target)

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
    "EvidencePruneResult",
    "EvidenceRecord",
    "EvidenceSnapshotState",
    "read_evidence_snapshot_state",
    "EvidenceSpoolCompactionResult",
    "EvidenceStore",
    "EvidenceStoreStats",
    "LinkAppendResult",
    "RefreshableUsageHead",
    "RefreshableUsageItem",
    "RefreshableUsageReconcileResult",
    "RefreshableUsageStats",
    "SpoolReplayResult",
]
