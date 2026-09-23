"""Disposable, bounded materialized receipts; never another raw event ledger.

One transaction publishes all entries and their common input generation. Reads
open SQLite read-only and cannot initialize a store. A failed publication leaves
the previous complete generation intact. The projector version is deliberately
separate from the public receipt wire schema.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .version import package_version

# A release upgrade must rebuild persisted reductions even when their public
# wire schema stays v1. Bump this manual revision for reducer, calibration or
# redaction semantics changed without a package release (including source
# checkouts, whose package identity may remain 0.0.0+source).
RECEIPT_PROJECTOR_REVISION = 1
RECEIPT_PROJECTOR_VERSION = f"agentacct:{package_version()}:receipt-projector:{RECEIPT_PROJECTOR_REVISION}"
MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_GENERATION_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 50_000
# These are encoded payload/entry limits, not a literal SQLite file-size cap.
# SQLite pages/indexes and a transactional WAL add overhead; freed pages are
# reused, and only one current generation's rows are retained.


@dataclass(frozen=True)
class SnapshotGeneration:
    generation_id: str
    input_token: str
    safety_token: str
    built_at: float
    published_at: float
    cpu_seconds: float
    projector_version: str
    store_identity: str
    entry_count: int
    payload_bytes: int


@dataclass(frozen=True)
class SnapshotEntry:
    kind: str
    key: str
    payload: dict[str, Any]
    generation: SnapshotGeneration

    @property
    def generation_id(self) -> str:
        return self.generation.generation_id

    @property
    def built_at(self) -> float:
        return self.generation.built_at

    @property
    def input_token(self) -> str:
        return self.generation.input_token

    @property
    def safety_token(self) -> str:
        return self.generation.safety_token


class ReceiptSnapshotStore:
    def __init__(
        self,
        store_dir: Path | str,
        *,
        projector_version: str = RECEIPT_PROJECTOR_VERSION,
        max_entry_bytes: int = MAX_ENTRY_BYTES,
        max_generation_bytes: int = MAX_GENERATION_BYTES,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.store_dir = Path(store_dir).expanduser().resolve()
        self.root = self.store_dir / "receipt-snapshots"
        self.path = self.root / "snapshots.sqlite3"
        self.projector_version = projector_version
        self.store_identity = hashlib.sha256(os.fsencode(self.store_dir)).hexdigest()
        self.max_entry_bytes = max_entry_bytes
        self.max_generation_bytes = max_generation_bytes
        self.max_entries = max_entries

    def _reader(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _generation(self, row: Mapping[str, Any]) -> SnapshotGeneration | None:
        if row["projector_version"] != self.projector_version or row["store_identity"] != self.store_identity:
            return None
        return SnapshotGeneration(**{name: row[name] for name in SnapshotGeneration.__dataclass_fields__})

    def generation(self) -> SnapshotGeneration | None:
        if not self.path.is_file():
            return None
        try:
            with self._reader() as connection:
                row = connection.execute("SELECT * FROM snapshot_generation WHERE singleton=1").fetchone()
                return self._generation(row) if row is not None else None
        except (sqlite3.Error, ValueError, KeyError, IndexError, TypeError):
            return None
        finally:
            if "connection" in locals():
                connection.close()

    def read(self, kind: str, key: str) -> SnapshotEntry | None:
        if not self.path.is_file():
            return None
        try:
            with self._reader() as connection:
                # One SELECT binds payload and metadata to the SAME SQLite read
                # snapshot even when another process publishes concurrently.
                row = connection.execute(
                    "SELECT g.*, e.payload FROM snapshot_entries e CROSS JOIN snapshot_generation g "
                    "WHERE g.singleton=1 AND e.kind=? AND e.key=?", (kind, key),
                ).fetchone()
                generation = self._generation(row) if row is not None else None
                if generation is None:
                    return None
                payload = json.loads(row["payload"])
                if not isinstance(payload, dict):
                    return None
                return SnapshotEntry(kind, key, payload, generation)
        except (sqlite3.Error, ValueError, KeyError, IndexError, TypeError):
            return None
        finally:
            if "connection" in locals():
                connection.close()

    def _writer(self) -> sqlite3.Connection:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA secure_delete=ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS snapshot_generation (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                generation_id TEXT NOT NULL, input_token TEXT NOT NULL,
                safety_token TEXT NOT NULL, built_at REAL NOT NULL, published_at REAL NOT NULL,
                cpu_seconds REAL NOT NULL, projector_version TEXT NOT NULL,
                store_identity TEXT NOT NULL, entry_count INTEGER NOT NULL,
                payload_bytes INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshot_entries (
                kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                content_hash TEXT NOT NULL, PRIMARY KEY(kind,key)
            );
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(snapshot_generation)")}
        if "published_at" not in columns:
            connection.execute("ALTER TABLE snapshot_generation ADD COLUMN published_at REAL NOT NULL DEFAULT 0")
        return connection

    def publish(
        self,
        entries: Mapping[tuple[str, str], Mapping[str, Any]],
        *,
        input_token: str,
        safety_token: str,
        built_at: float | None = None,
        published_at: float | None = None,
        cpu_seconds: float = 0.0,
    ) -> SnapshotGeneration:
        """Atomically replace the current generation; retain no history blobs."""
        publication_cpu_started = time.process_time()
        moment = time.time() if built_at is None else built_at
        if (not math.isfinite(moment) or not math.isfinite(cpu_seconds) or cpu_seconds < 0
                or (published_at is not None and not math.isfinite(published_at))):
            raise ValueError("invalid snapshot timing")
        if not all(isinstance(token, str) and 0 < len(token) <= 4096 for token in (input_token, safety_token)):
            raise ValueError("snapshot tokens must be nonempty bounded strings")
        if len(entries) > self.max_entries:
            raise ValueError("snapshot entry limit exceeded")
        rows: list[tuple[str, str, str, str]] = []
        payload_bytes = 0
        for (kind, key), payload in entries.items():
            if not isinstance(kind, str) or not kind or len(kind) > 128 or not isinstance(key, str) or len(key) > 4096:
                raise ValueError("invalid snapshot entry key")
            if not isinstance(payload, Mapping):
                raise ValueError("snapshot payload must be an object")
            serialized = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
            encoded = serialized.encode("utf-8")
            payload_bytes += len(encoded)
            if len(encoded) > self.max_entry_bytes or payload_bytes > self.max_generation_bytes:
                raise ValueError("snapshot payload limit exceeded")
            rows.append((kind, key, serialized, hashlib.sha256(encoded).hexdigest()))
        generation = SnapshotGeneration(
            uuid.uuid4().hex, input_token, safety_token, moment,
            time.time() if published_at is None else published_at, cpu_seconds,
            self.projector_version, self.store_identity, len(rows), payload_bytes,
        )
        connection = self._writer()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TEMP TABLE incoming_keys (kind TEXT,key TEXT,PRIMARY KEY(kind,key))")
                connection.executemany("INSERT INTO incoming_keys VALUES (?,?)", [(r[0], r[1]) for r in rows])
                connection.executemany(
                    "INSERT INTO snapshot_entries VALUES (?,?,?,?) ON CONFLICT(kind,key) DO UPDATE "
                    "SET payload=excluded.payload,content_hash=excluded.content_hash "
                    "WHERE snapshot_entries.content_hash<>excluded.content_hash", rows,
                )
                connection.execute("DELETE FROM snapshot_entries WHERE NOT EXISTS "
                                   "(SELECT 1 FROM incoming_keys i WHERE i.kind=snapshot_entries.kind AND i.key=snapshot_entries.key)")
                # Include JSON encoding and SQL mutation in the worker CPU
                # receipt, not only the caller's upstream projection reduce.
                generation = SnapshotGeneration(
                    generation.generation_id, input_token, safety_token, moment,
                    time.time() if published_at is None else published_at,
                    cpu_seconds + max(0.0, time.process_time() - publication_cpu_started),
                    self.projector_version, self.store_identity, len(rows), payload_bytes,
                )
                connection.execute(
                    "INSERT OR REPLACE INTO snapshot_generation "
                    "(singleton,generation_id,input_token,safety_token,built_at,published_at,cpu_seconds,"
                    "projector_version,store_identity,entry_count,payload_bytes) VALUES (1,?,?,?,?,?,?,?,?,?,?)",
                    tuple(getattr(generation, name) for name in SnapshotGeneration.__dataclass_fields__),
                )
            # Bound retained WAL history when no reader holds an old generation;
            # a concurrent fast reader may defer this checkpoint, never publication.
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                # Publication is already durable. A housekeeping failure must
                # not report the complete committed generation as a failed write.
                pass
            return generation
        finally:
            connection.close()

    def clear(self, *, expected_generation_id: str | None = None) -> bool:
        """Invalidate a known unsafe generation without deleting a newer one."""
        if not self.path.is_file():
            return False
        connection = sqlite3.connect(self.path, timeout=0.1)
        try:
            connection.execute("PRAGMA secure_delete=ON")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT generation_id FROM snapshot_generation WHERE singleton=1").fetchone()
                if row is None or (expected_generation_id is not None and row[0] != expected_generation_id):
                    return False
                connection.execute("DELETE FROM snapshot_entries")
                connection.execute("DELETE FROM snapshot_generation")
            return True
        finally:
            connection.close()
