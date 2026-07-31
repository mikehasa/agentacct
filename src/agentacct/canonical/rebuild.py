"""Build a fresh live canonical store (``chronicle.sqlite3``) from events.jsonl.

The append-only ``events.jsonl`` ledger is the source of truth; the canonical
SQLite store is a rebuildable index over it. Nothing populated the XDG store's
canonical index yet (the shadow writer only captures events GOING FORWARD, and
the production cutover is a candidate -> parity -> promote ceremony), so the
fast read path (``CanonicalReadRuntime``) has no store to serve. This module is
the missing one-shot bridge: it runs the read-only legacy importer over a
verified snapshot of the ledger — which also materializes the ``rm_task_current``
and ``rm_usage_day`` read models — and installs the result at the reserved live
name.

The install is the MECHANICAL effect of a phase-5 promotion (copy the candidate
to ``chronicle.sqlite3``, flip its role to ``live``, give it the identity
``open_live`` demands) WITHOUT the parity-report/writers-stopped ceremony. That
is appropriate for a local/dev rebuild precisely because the JSONL remains the
authority: the store can be thrown away and rebuilt from it at any time. It is
NOT the authoritative cutover path — that stays ``canonical promote``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .legacy_import import import_legacy_snapshot
from .snapshot import VerifiedSnapshot
from .sqlite import LIVE_STORE_FILENAME, CanonicalStore

# The ledger is always recorded under this name inside a store directory, and
# the importer resolves it by this manifest-declared name inside the snapshot.
EVENTS_FILENAME = "events.jsonl"


class RebuildError(RuntimeError):
    """A rebuild refused to produce a store (e.g. nothing importable)."""


@dataclass(frozen=True)
class RebuildReport:
    """What a rebuild produced, for the CLI and callers to report honestly."""

    store_path: Path
    parsed_events: int
    session_count: int
    task_count: int
    usage_day_count: int
    issue_count: int
    canonical_sequence: int


def _verified_events_snapshot(scratch: Path, content: bytes) -> VerifiedSnapshot:
    """Stage a byte-for-byte verified snapshot of the ledger in ``scratch``.

    Mirrors the test snapshot recipe: the payload lives in its own root and the
    manifest (declaring the exact size + sha256) lives OUTSIDE that root, which
    ``VerifiedSnapshot.verify`` requires.
    """

    root = scratch / "snapshot"
    root.mkdir(mode=0o700)
    (root / EVENTS_FILENAME).write_bytes(content)
    manifest = scratch / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "legacy-chronicle",
                "files": [
                    {
                        "path": EVENTS_FILENAME,
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return VerifiedSnapshot.verify(root=root.resolve(), manifest=manifest.resolve())


def _install_live(candidate_path: Path, store_dir: Path) -> Path:
    """Place a freshly imported candidate at the reserved live name.

    Stages the copy in the destination directory and swaps it in with a single
    atomic ``os.replace`` so a concurrent reader never observes a torn file. The
    candidate's role is flipped to ``live`` before the swap; the file is given
    the 0600 identity ``open_live`` demands.
    """

    store_dir.mkdir(parents=True, exist_ok=True)
    target = store_dir / LIVE_STORE_FILENAME
    descriptor, staged_name = tempfile.mkstemp(prefix=".chronicle-rebuild-", suffix=".tmp", dir=store_dir)
    os.close(descriptor)
    staged = Path(staged_name)
    try:
        shutil.copy2(candidate_path, staged)
        connection = sqlite3.connect(staged)
        try:
            connection.execute("UPDATE store_metadata SET store_role = 'live' WHERE singleton = 1")
            connection.commit()
        finally:
            connection.close()
        os.chmod(staged, 0o600)
        os.replace(staged, target)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    # The freshly installed store was checkpointed (TRUNCATE) and copied as a
    # single file, so it owns no WAL. Any -wal/-shm next to the reserved name
    # can only be STALE sidecars left by a previous store; SQLite validates a
    # mismatched WAL and does not replay it, but leaving one attached to a new
    # main file is undefined-adjacent — remove them so the store opens clean.
    for suffix in ("-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    return target


def rebuild_live_store_from_events(store_dir: Path | str) -> RebuildReport:
    """Rebuild ``<store_dir>/chronicle.sqlite3`` from ``<store_dir>/events.jsonl``.

    Raises ``FileNotFoundError`` if the ledger is absent. Any existing live
    store is replaced (the ledger is the authority).
    """

    store_dir = Path(store_dir).expanduser()
    events_path = store_dir / EVENTS_FILENAME
    if not events_path.is_file():
        raise FileNotFoundError(f"no events ledger at {events_path}")
    content = events_path.read_bytes()

    # Resolve symlink components (e.g. macOS /var -> /private/var): the
    # importer's candidate path is validated to contain no symlink component.
    scratch = Path(tempfile.mkdtemp(prefix="agentacct-rebuild-")).resolve(strict=True)
    try:
        snapshot = _verified_events_snapshot(scratch, content)
        candidate_path = scratch / "candidate.sqlite3"
        candidate = CanonicalStore.create(candidate_path)
        try:
            report = import_legacy_snapshot(
                snapshot=snapshot,
                store=candidate,
                scratch_root=scratch,
                source_file=EVENTS_FILENAME,
            )
            if int(report.parsed_events) == 0:
                # Nothing imported (e.g. a truncated ledger, or one carrying only
                # events without a source_namespace_fingerprint). Standing up an
                # empty-but-valid role='live' store would let a later read-flag
                # flip serve a store that silently under-reports. Refuse loudly
                # instead — the ledger is intact and re-running after a fix
                # recovers fully. Fail visible, like the read runtime itself.
                raise RebuildError(
                    "no importable events found in "
                    f"{store_dir / EVENTS_FILENAME} "
                    f"({report.migration_issue_count} lines excluded, e.g. "
                    "missing source_namespace_fingerprint); refusing to install "
                    "an empty live store"
                )
            session_count = int(
                candidate.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            )
            # Fold the WAL into the main database so the single-file copy the
            # install performs is complete.
            candidate.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            candidate.close()
        target = _install_live(candidate_path, store_dir)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    projection = report.projection or {}
    return RebuildReport(
        store_path=target,
        parsed_events=int(report.parsed_events),
        session_count=session_count,
        task_count=int(projection.get("task_count", 0)),
        usage_day_count=int(projection.get("usage_day_count", 0)),
        issue_count=int(report.migration_issue_count),
        canonical_sequence=int(report.canonical_sequence_after),
    )


__all__ = [
    "EVENTS_FILENAME",
    "RebuildError",
    "RebuildReport",
    "rebuild_live_store_from_events",
]
