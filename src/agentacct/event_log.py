"""SQLite raw event log — the substrate that will replace ``events.jsonl``.

``events.jsonl`` is today's authoritative append-only ledger. This module is a
faithful SQLite mirror of it: one row per ledger line, stored VERBATIM and in
file order, so the two are provably byte-identical line for line. That parity is
the whole point — it is what lets writes flip to SQLite and the flat file be
deleted without losing a single event (the owner's step 3: prove they are the
same).

Design contract during the mirror phase:

* **Faithful, not clever.** A row stores the exact serialized line. Corrupt /
  non-object lines a whole-file rewrite carries through verbatim are stored
  verbatim too, so the mirror never diverges from the file it shadows.
* **Fail-open on write.** The flat file stays authoritative while both exist;
  a mirror hiccup must never fail a proven v1 event write (the same contract the
  canonical/evidence shadows already hold). Drift is healed by
  :meth:`reconcile_from_file` on the next open and caught by
  :meth:`verify_against_file`.
* **Query-ready.** Alongside the verbatim line, the indexed columns
  (``event_id``, ``run_id``, ``event_type``, ``created_at``) are extracted so a
  later read-side cutover can filter in SQL instead of re-parsing the whole
  ledger on every read.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .env_compat import read_env_alias

RAW_EVENT_LOG_FILENAME = "events.sqlite3"
# Persistent, store-scoped proof that this store has been cut over: its ledger
# is the SQLite log and events.jsonl is gone. Written by the cutover so that
# authoritativeness survives process restarts and does NOT depend on a volatile
# env var (a mirror-mode open of a cut-over store would otherwise wipe the log).
AUTHORITATIVE_MARKER_FILENAME = "events.authoritative"

# The SQLite log is the AUTHORITATIVE event store: every ledger read and write
# goes to it and the flat events.jsonl is neither read nor written (and can be
# deleted). Default ON — a fresh store is SQLite-only from the first event, and
# an existing events.jsonl store auto-adopts the log on open (reconciling the log
# from the file, then leaving the file behind as a backup). Set
# AGENTACCT_EVENT_LOG_AUTHORITATIVE=0 (or false/no/off) to opt back into the
# legacy flat-file mode, where events.jsonl stays authoritative and the log is a
# proven mirror. That opt-out exists for the mirror-mode/parity tests and for
# recovering a store on an older runtime.
EVENT_LOG_AUTHORITATIVE_ENV = "AGENTACCT_EVENT_LOG_AUTHORITATIVE"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def event_log_authoritative() -> bool:
    """Whether the SQLite event log is the authoritative ledger (the default).

    True unless ``AGENTACCT_EVENT_LOG_AUTHORITATIVE`` is set to an explicit
    falsey value (``0``/``false``/``no``/``off``). Unset — and any unrecognized
    value — means authoritative: the flip to SQLite is the product default, and
    only a deliberate opt-out returns to the flat-file mirror mode.
    """

    value = read_env_alias(EVENT_LOG_AUTHORITATIVE_ENV)
    return str(value or "").strip().lower() not in _FALSE_VALUES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_lines (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    run_id TEXT,
    event_type TEXT,
    created_at REAL,
    line TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_lines_run ON event_lines(run_id);
CREATE INDEX IF NOT EXISTS idx_event_lines_event_id ON event_lines(event_id);
-- Durable record of every flat-file line already drained into the log during a
-- rolling upgrade (see absorb_new_events). Its keys OUTLIVE a log rewrite that
-- removes the row, which is what stops a deliberately-removed event from being
-- resurrected out of the frozen events.jsonl backup on a later absorb.
CREATE TABLE IF NOT EXISTS absorbed_flat (key TEXT PRIMARY KEY);
"""


def serialize_event(event: dict[str, Any]) -> str:
    """The canonical ledger serialization of one event.

    Must match exactly what the flat-file writers emit
    (``json.dumps(event, sort_keys=True)``), so a mirror row is byte-identical
    to the line the file would carry.
    """

    return json.dumps(event, sort_keys=True)


@dataclass(frozen=True)
class ParityResult:
    """Outcome of comparing the log against the flat file, line for line."""

    matches: bool
    file_lines: int
    log_lines: int
    first_divergence: int | None  # 0-based index of the first differing line
    detail: str


def _extract_columns(line: str) -> tuple[str | None, str | None, str | None, float | None]:
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None, None, None, None
    if not isinstance(event, dict):
        return None, None, None, None
    event_id = event.get("event_id")
    run_id = event.get("run_id")
    event_type = event.get("event_type")
    created_at = event.get("created_at")
    return (
        event_id if isinstance(event_id, str) else None,
        run_id if isinstance(run_id, str) else None,
        event_type if isinstance(event_type, str) else None,
        float(created_at) if isinstance(created_at, (int, float)) else None,
    )


def _read_file_lines(events_path: Path) -> list[str]:
    """Ledger lines in file order, dropping only blank lines.

    Mirrors ``_partition_existing_for_rewrite``'s preservation rule (keep
    corrupt-but-recoverable bytes) rather than ``_read_events_file_order``'s
    silent drop, so the mirror is faithful to every non-blank line.
    """

    if not events_path.exists():
        return []
    return [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]


class RawEventLog:
    """A faithful SQLite mirror of the flat event ledger."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser()
        # Open per operation (no persistent handle): the service constructs one
        # RawEventLog per store and may never close it, so a lifetime-held
        # connection would leak a file descriptor per service. sqlite's own
        # file locking serializes concurrent writers.
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            with connection:
                yield connection
        finally:
            connection.close()

    # -- writes -------------------------------------------------------------

    def append_line(self, line: str) -> None:
        """Append one verbatim ledger line (with its indexed columns)."""

        event_id, run_id, event_type, created_at = _extract_columns(line)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO event_lines(event_id, run_id, event_type, created_at, line) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_id, run_id, event_type, created_at, line),
            )

    def append_event(self, event: dict[str, Any]) -> None:
        """Append one event, serialized exactly as the flat file would."""

        self.append_line(serialize_event(event))

    def replace_all(self, lines: Iterable[str]) -> None:
        """Rebuild the whole log to exactly ``lines`` (for a file rewrite)."""

        rows = [(*_extract_columns(line), line) for line in lines]
        with self._connect() as connection:
            connection.execute("DELETE FROM event_lines")
            connection.execute("DELETE FROM sqlite_sequence WHERE name='event_lines'")
            connection.executemany(
                "INSERT INTO event_lines(event_id, run_id, event_type, created_at, line) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    # -- reads --------------------------------------------------------------

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM event_lines").fetchone()[0])

    def read_lines(self) -> list[str]:
        with self._connect() as connection:
            return [
                str(row[0])
                for row in connection.execute("SELECT line FROM event_lines ORDER BY seq")
            ]

    def read_events(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        """Parsed events in file order, matching ``_read_events_file_order``:
        blank/corrupt lines are skipped, and ``run_id`` filters if given."""

        if run_id is None:
            rows = self.read_lines()
        else:
            with self._connect() as connection:
                rows = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT line FROM event_lines WHERE run_id = ? ORDER BY seq",
                        (run_id,),
                    )
                ]
        events: list[dict[str, Any]] = []
        for line in rows:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    # -- reconcile / parity -------------------------------------------------

    def reconcile_from_file(self, events_path: Path) -> ParityResult:
        """Bring the log into exact agreement with the flat file.

        Fast path: the file was only appended to (log lines are an exact prefix
        of the file lines) — insert just the new tail. Otherwise (rewrite,
        divergence, empty log) rebuild wholesale. Returns the post-reconcile
        parity result, which must always match.
        """

        if not events_path.exists():
            # No source to reconcile from. If the log already holds data, this
            # is the authoritative-store-opened-as-mirror hazard: replacing the
            # log with the empty file would DESTROY the ledger. Refuse — keep
            # the log intact. A genuinely fresh store has an empty log here.
            log_lines = self.read_lines()
            if log_lines:
                return ParityResult(False, 0, len(log_lines), None, "source_absent_log_preserved")
            return ParityResult(True, 0, 0, None, "both_empty")
        file_lines = _read_file_lines(events_path)
        log_lines = self.read_lines()
        if log_lines == file_lines:
            return ParityResult(True, len(file_lines), len(log_lines), None, "in_sync")
        if len(log_lines) < len(file_lines) and file_lines[: len(log_lines)] == log_lines:
            # Pure append since last sync — add only the new tail.
            with self._connect() as connection:
                connection.executemany(
                    "INSERT INTO event_lines(event_id, run_id, event_type, created_at, line) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(*_extract_columns(line), line) for line in file_lines[len(log_lines):]],
                )
        else:
            self.replace_all(file_lines)
        return self.verify_against_file(events_path)

    def absorb_new_events(self, events_path: Path) -> int:
        """Append flat-file events the log has never absorbed; return the count.

        The rolling-upgrade drain. After a store cuts over to the log, a
        not-yet-restarted OLD mirror-mode process (a 0.5.x daemon that predates
        the SQLite log) may keep writing to events.jsonl — appending new events,
        or even rewriting the whole file. Those events must reach the
        authoritative log or a rolling upgrade would split writes between the two
        ledgers, but events.jsonl is left INTACT (a read/dry-run of a store must
        not mutate it, and it stays a backup until the deliberate cutover).

        Idempotence and no-resurrection come from a durable membership set,
        ``absorbed_flat``, keyed by event_id (``id:<eid>``) or, for a
        corrupt/legacy line with no id, a content hash (``ln:<sha1>``). On the
        FIRST sighting of a flat-file event its key is recorded — WHETHER OR NOT
        it is appended. An event already in the log is not re-appended, but its
        key is still recorded, because it may have reached the log via a path
        that does not populate ``absorbed_flat`` (a mirror-mode
        ``reconcile_from_file``, or a restored older store) — every pre-existing
        store upgrades through exactly that state. Because ``absorbed_flat``
        OUTLIVES a log rewrite (redaction / dedup / usage-refresh replace) that
        deletes the row, an event the log deliberately removed is never
        re-absorbed from the frozen file. And because membership is by
        id/content, not by file position, an old process rewriting or reordering
        events.jsonl cannot smuggle an already-seen event back in or hide a
        genuinely new one.

        Known limit (union, never truncation): an old process DELETING a row from
        events.jsonl during the transition is not propagated to the log — the row
        lingers until the deliberate cutover. Usage accounting dedupes superseded
        snapshots by session identity, and the window closes when the old
        processes are restarted, so this does not corrupt totals in practice.
        """

        if not events_path.exists():
            return 0
        file_lines = _read_file_lines(events_path)
        if not file_lines:
            return 0
        with self._connect() as connection:
            log_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT event_id FROM event_lines WHERE event_id IS NOT NULL"
                )
            }
            absorbed = {str(row[0]) for row in connection.execute("SELECT key FROM absorbed_flat")}
            log_lines_set: set[str] | None = None  # log's verbatim lines, lazily, for id-less dedup
            to_add: list[str] = []
            new_keys: list[str] = []
            for line in file_lines:
                event_id, *_ = _extract_columns(line)
                if event_id is not None:
                    key = "id:" + event_id
                    already_in_log = event_id in log_ids
                else:
                    key = "ln:" + hashlib.sha1(line.encode("utf-8")).hexdigest()
                    if log_lines_set is None:
                        log_lines_set = {
                            str(row[0]) for row in connection.execute("SELECT line FROM event_lines")
                        }
                    already_in_log = line in log_lines_set
                if key in absorbed:
                    continue  # already recorded as seen-from-the-flat-file
                # First sighting of this event from events.jsonl. RECORD its key
                # unconditionally — even when it is already in the log via a
                # mirror-mode reconcile or a restore (which do NOT populate
                # absorbed_flat) — so that if a later rewrite removes the row, a
                # subsequent absorb cannot resurrect it from the retained backup.
                # Append to the log only when it is not already present.
                absorbed.add(key)
                new_keys.append(key)
                if not already_in_log:
                    to_add.append(line)
                    if event_id is None and log_lines_set is not None:
                        log_lines_set.add(line)
            if to_add:
                connection.executemany(
                    "INSERT INTO event_lines(event_id, run_id, event_type, created_at, line) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(*_extract_columns(line), line) for line in to_add],
                )
            if new_keys:
                connection.executemany(
                    "INSERT OR IGNORE INTO absorbed_flat(key) VALUES (?)",
                    [(key,) for key in new_keys],
                )
        return len(to_add)

    def verify_against_file(self, events_path: Path) -> ParityResult:
        """Compare the log to the flat file line for line — the parity proof."""

        file_lines = _read_file_lines(events_path)
        log_lines = self.read_lines()
        if log_lines == file_lines:
            return ParityResult(True, len(file_lines), len(log_lines), None, "match")
        first = next(
            (
                index
                for index in range(min(len(file_lines), len(log_lines)))
                if file_lines[index] != log_lines[index]
            ),
            min(len(file_lines), len(log_lines)),
        )
        return ParityResult(
            False,
            len(file_lines),
            len(log_lines),
            first,
            f"diverged at line {first} (file={len(file_lines)}, log={len(log_lines)})",
        )


__all__ = [
    "AUTHORITATIVE_MARKER_FILENAME",
    "EVENT_LOG_AUTHORITATIVE_ENV",
    "RAW_EVENT_LOG_FILENAME",
    "ParityResult",
    "RawEventLog",
    "event_log_authoritative",
    "serialize_event",
]
