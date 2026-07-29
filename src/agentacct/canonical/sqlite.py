"""SQLite implementation for the isolated canonical truth-model spike.

Nothing in this module selects a default store path. Callers must provide an
explicit candidate database path, and the current agentacct runtime does not
import this package.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .live_paths import configured_live_state_roots, paths_overlap
from .v1_events import session_observation_hash_material
from .types import (
    CostCalculationInput,
    FactInput,
    FactSessionLinkInput,
    RECOVERY_FACT_TRANSPORT,
    RECOVERY_LINK_METHOD,
    RECOVERY_LINK_RULE_VERSION,
    RecoveredWorkClaimInput,
    Session,
    SessionEdgeInput,
    SessionInput,
    SourceInstance,
    SourceInstanceInput,
    TaskAnchor,
    TaskCurrent,
    TaskCursor,
    UsageDayQuery,
    UsageMeasurementInput,
    WorkClaimInput,
    WriteResult,
    dataclass_dict,
)


APPLICATION_ID = 0x41434354  # "ACCT"
# Version 1 was the spike's unstable DDL (it drifted while the number stood
# still); version 2 is the first checksum-pinned schema; version 3 admits the
# 'live' store role for the writer-integration phase; version 4 adds the
# session_observed_lineage absorb-state table. v1 candidates are rebuilt
# from a fresh snapshot, never migrated; v2+ stores migrate forward.
SCHEMA_VERSION = 4
SCHEMA_BASE_NAME = "canonical-v4"
APP_VERSION_DEFAULT = "sqlite-truth-spike"
MINIMUM_SQLITE_VERSION = (3, 37, 0)
TASK_PUBLIC_ID_PREFIX = "task_"
LIVE_STORE_FILENAME = "chronicle.sqlite3"
_MAX_SQLITE_INT = 9_223_372_036_854_775_807
# Bare directory names, not <name>/state: live state also lives beside state/
# (policy.yaml) and the -global root itself serves as a hooks project-dir.
_FORBIDDEN_STATE_SEQUENCES = (
    (".agent-sentinel",),
    (".agent-sentinel-global",),
    (".agent-chronicle",),
    (".agent-chronicle-global",),
)

_GET_TASK_SQL = "SELECT * FROM rm_task_current WHERE public_task_id = ?"
_LIST_TASKS_SQL = (
    "SELECT * FROM rm_task_current INDEXED BY idx_rm_task_work_recent "
    "WHERE (sort_activity_at_us, public_task_id) < (?, ?) "
    "ORDER BY sort_activity_at_us DESC, public_task_id DESC LIMIT ?"
)
_LIST_SESSIONS_SQL = (
    "SELECT * FROM sessions INDEXED BY idx_sessions_recent "
    "WHERE (sort_activity_at_us, session_id) < (?, ?) "
    "ORDER BY sort_activity_at_us DESC, session_id DESC LIMIT ?"
)
# Batched point lookups for surface labeling (phase 4.2): each resolves by
# primary key / a pinned index over an id list bounded by the page the caller
# just fetched. The placeholder count varies with the batch; the shape does
# not.
_SESSIONS_BY_IDS_SQL = "SELECT * FROM sessions WHERE session_id IN ({placeholders})"
_SOURCES_BY_IDS_SQL = (
    "SELECT * FROM source_instances WHERE source_instance_id IN ({placeholders})"
)
# At most one row per child: uq_session_edges_one_valid_parent is
# relation-agnostic (one valid edge per child, whatever its relation), and
# writers mint the relation from the child's session kind — 'continuation'
# and 'retry' children carry their single validated lineage edge under those
# relations, so filtering to relation='parent' would silently drop them.
_VALID_LINEAGE_EDGES_SQL = (
    "SELECT child_session_id, parent_session_id, relation "
    "FROM session_edges INDEXED BY idx_session_edges_child "
    "WHERE child_session_id IN ({placeholders}) "
    "AND validation_state = 'valid'"
)
# Bound for every by-ids batch: one page of the widest list surface.
_BY_IDS_BATCH_LIMIT = 2_000
_HEALTH_STORE_SQL = (
    "SELECT store_uuid, store_role, schema_version, canonical_sequence "
    "FROM store_metadata WHERE singleton = 1"
)
# The single authority for which rm_* read models the minimal rebuild
# maintains. The rebuild policy (canonical_live) and the health probe
# (canonical_read) both derive from this tuple — a new read model added to
# rebuild_minimal_read_models without updating this constant would silently
# escape staleness detection and never_built synthesis.
MINIMAL_READ_MODEL_NAMES = ("rm_task_current", "rm_usage_day")
_HEALTH_PROJECTIONS_SQL = (
    "SELECT projection_name, projection_version, built_through_sequence, state "
    "FROM projection_generations "
    # Static, repo-controlled identifiers — not user input.
    f"WHERE projection_name IN ({', '.join(repr(name) for name in MINIMAL_READ_MODEL_NAMES)}) "
    f"ORDER BY projection_name LIMIT {len(MINIMAL_READ_MODEL_NAMES)}"
)
_HEALTH_SOURCES_SQL = (
    "SELECT source_instance_id, status, last_success_at_us, reason_code "
    "FROM source_health_current WHERE source_instance_id > 0 "
    "ORDER BY source_instance_id LIMIT 100"
)


def now_us() -> int:
    return time.time_ns() // 1_000


@dataclass(frozen=True)
class SchemaMigration:
    """One forward schema step from ``from_version`` to ``from_version + 1``.

    Statements run one by one inside a single transaction (never via
    ``executescript``, which would commit early) and must produce exactly the
    DDL that compiling the target build's packaged ``schema.sql`` produces —
    the open-time inventory comparison enforces that equivalence.
    """

    from_version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        digest = hashlib.sha256(b"agent-chronicle-schema-migration-v1\x00")
        for statement in self.statements:
            payload = statement.encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
        return digest.hexdigest()


# Forward steps keyed by from_version. Version 1 was the spike's unstable
# DDL, so v1 candidates rebuild rather than migrate. Statements are frozen
# literals: the registered checksum is validated against every previously
# migrated store on open, so a step may never be regenerated from the moving
# schema.sql — the inventory comparison is what proves the literal still
# produces the packaged DDL.
SCHEMA_MIGRATIONS: dict[int, SchemaMigration] = {
    2: SchemaMigration(
        from_version=2,
        name="allow-live-store-role",
        statements=(
            # SQLite cannot alter a CHECK constraint in place; rebuild the
            # singleton table around the copied row. The final CREATE must
            # stay byte-identical to the store_metadata block in schema.sql.
            "CREATE TABLE store_metadata_role_migration AS "
            "SELECT singleton, store_uuid, store_role, schema_version, "
            "canonical_sequence, created_at_us FROM store_metadata",
            "DROP TABLE store_metadata",
            (
                "CREATE TABLE store_metadata (\n"
                "    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\n"
                "    store_uuid TEXT NOT NULL UNIQUE,\n"
                "    store_role TEXT NOT NULL CHECK (store_role IN ('candidate', 'live')),\n"
                "    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),\n"
                "    canonical_sequence INTEGER NOT NULL DEFAULT 0 CHECK (canonical_sequence >= 0),\n"
                "    created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0)\n"
                ") STRICT"
            ),
            "INSERT INTO store_metadata(singleton, store_uuid, store_role, "
            "schema_version, canonical_sequence, created_at_us) "
            "SELECT singleton, store_uuid, store_role, schema_version, "
            "canonical_sequence, created_at_us FROM store_metadata_role_migration",
            "DROP TABLE store_metadata_role_migration",
        ),
    ),
    3: SchemaMigration(
        from_version=3,
        name="add-session-observed-lineage",
        statements=(
            # Purely additive; the CREATE must stay byte-identical to the
            # session_observed_lineage block in schema.sql.
            (
                "CREATE TABLE session_observed_lineage (\n"
                "    session_id INTEGER PRIMARY KEY REFERENCES sessions(session_id),\n"
                "    parent_client TEXT NOT NULL CHECK (length(parent_client) BETWEEN 1 AND 64),\n"
                "    parent_client_session_id TEXT NOT NULL CHECK (length(parent_client_session_id) BETWEEN 1 AND 512),\n"
                "    parent_namespace_digest BLOB NOT NULL CHECK (length(parent_namespace_digest) = 32),\n"
                "    updated_at_us INTEGER NOT NULL CHECK (updated_at_us >= 0)\n"
                ") STRICT"
            ),
        ),
    ),
}

# (name, sha256-of-packaged-schema.sql) for every SUPERSEDED checksum-pinned
# schema version, so this build can keep attesting the birth row of stores
# born on older builds — the migrated-store branch otherwise has nothing to
# check that row against. Version 1 is absent deliberately (the spike DDL
# drifted under one number, so v1 birth rows attest nothing). When
# SCHEMA_VERSION moves again, append the outgoing schema.sql's digest here.
HISTORICAL_SCHEMA_CHECKSUMS: dict[int, tuple[str, str]] = {
    2: (
        "canonical-v2",
        "14f356a010c50acd85e2a85b174fd374a86affeafafd196cd86063fc8e806c70",
    ),
    3: (
        "canonical-v3",
        "cce3f34d711a171d1e9a490fe1fb50f9adc93e21b5683e319dc3fc1f56c805dc",
    ),
}


def _apply_schema_migrations(
    connection: sqlite3.Connection,
    *,
    from_version: int,
    app_version: str,
) -> int:
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if journal_mode != "wal":
        raise ValueError("candidate database is not in WAL mode; refusing to migrate")
    metadata_version = connection.execute(
        "SELECT schema_version FROM store_metadata WHERE singleton = 1"
    ).fetchone()
    if metadata_version is None or int(metadata_version[0]) < from_version:
        raise ValueError("candidate metadata disagrees with the schema version; refusing to migrate")
    current = from_version
    while current < SCHEMA_VERSION:
        step = SCHEMA_MIGRATIONS.get(current)
        if step is None:
            raise ValueError(
                f"candidate schema version {current} has no upgrade path in "
                f"this build; rebuild the candidate from a fresh snapshot"
            )
        if step.from_version != current:
            raise ValueError(
                f"schema migration '{step.name}' is registered for version "
                f"{current} but declares from_version {step.from_version}"
            )
        target = current + 1
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise ValueError(
                f"schema migration '{step.name}' to version {target} could not "
                f"acquire the write lock; another process may be migrating"
            ) from exc
        try:
            # Re-read versions under the exclusive lock: a concurrent writable
            # open may have finished this step (or the whole chain) while we
            # waited. Adopting its committed progress keeps racing opens
            # idempotent instead of failing on the stamping UNIQUE constraint.
            observed = int(connection.execute("PRAGMA user_version").fetchone()[0])
            row = connection.execute(
                "SELECT schema_version FROM store_metadata WHERE singleton = 1"
            ).fetchone()
            stamped = None if row is None else int(row[0])
            if observed != current or stamped != current:
                if stamped == observed and current < observed <= SCHEMA_VERSION:
                    connection.execute("ROLLBACK")
                    current = observed
                    continue
                raise ValueError(
                    "candidate metadata disagrees with the schema version; refusing to migrate"
                )
            for statement in step.statements:
                connection.execute(statement)
                if not connection.in_transaction:
                    # A statement that is itself COMMIT/ROLLBACK/END would
                    # make the stamping writes below autocommit one by one,
                    # silently breaking per-step atomicity.
                    raise ValueError(
                        f"schema migration '{step.name}' statement ended the "
                        f"transaction; step atomicity cannot be proved"
                    )
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, app_version, applied_at_us) "
                "VALUES (?, ?, ?, ?, ?)",
                (target, step.name, step.checksum, app_version, now_us()),
            )
            connection.execute(
                "UPDATE store_metadata SET schema_version = ? WHERE singleton = 1",
                (target,),
            )
            connection.execute(f"PRAGMA user_version = {target}")
            connection.execute("COMMIT")
        except BaseException as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            if isinstance(exc, sqlite3.Error):
                # A distinct error class keeps migration failures out of the
                # generic unreadable-DDL diagnosis in open().
                raise ValueError(
                    f"schema migration '{step.name}' to version {target} failed"
                ) from exc
            raise
        current = target
    return current


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical hash value: {type(value).__name__}")


def canonical_hash(value: Any) -> bytes:
    encoded = json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def _schema_inventory(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    """Return every application-defined schema object and its exact DDL."""

    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
        "ORDER BY type, name"
    ).fetchall()
    return tuple(
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in rows
    )


def _expected_schema_inventory(schema_sql: str) -> tuple[tuple[str, str, str, str], ...]:
    """Compile the packaged schema so stored metadata cannot attest altered DDL."""

    expected = sqlite3.connect(":memory:", isolation_level=None)
    try:
        expected.executescript(schema_sql)
        return _schema_inventory(expected)
    finally:
        expected.close()


def _reject_live_store_shape(path: Path, *, resolve_path: bool = True) -> None:
    candidates = [path]
    if resolve_path:
        candidates.append(path.resolve(strict=False))
    live_roots = configured_live_state_roots()
    for candidate in candidates:
        folded = tuple(part.casefold() for part in candidate.parts)
        if ".codex" in folded:
            raise ValueError("candidate database cannot be inside a live Codex state path")
        for sequence in _FORBIDDEN_STATE_SEQUENCES:
            width = len(sequence)
            if any(
                folded[index : index + width] == sequence
                for index in range(len(folded) - width + 1)
            ):
                raise ValueError(
                    "candidate database cannot be created inside live agentacct state"
                )
        for live_root in live_roots:
            if paths_overlap(candidate, live_root):
                if live_root in _configured_live_codex_roots():
                    raise ValueError(
                        "candidate database cannot overlap the configured live Codex state"
                    )
                raise ValueError(
                    "candidate database must be disjoint from every configured live state root"
                )
    if path.name in {
        "events.jsonl",
        "projection.sqlite3",
        "chronicle.sqlite3",
        "agent-chronicle.sqlite3",
    }:
        raise ValueError("candidate database path looks like a legacy live-store file")


def _configured_live_codex_roots() -> tuple[Path, ...]:
    roots = {(Path.home() / ".codex").resolve(strict=False)}
    configured = os.environ.get("CODEX_HOME")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        roots.add(candidate.resolve(strict=False))
    return tuple(roots)


def _assert_no_symlink_components(path: Path, *, include_leaf: bool) -> None:
    inspected = path if include_leaf else path.parent
    current = Path(inspected.anchor)
    for part in inspected.parts[1:]:
        current = current / part
        try:
            observed = current.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"candidate path component does not exist: {current}") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise ValueError(f"candidate path may not contain symlink components: {current}")


def _verify_opened_store(
    connection: sqlite3.Connection,
    *,
    read_only: bool,
    expected_role: str,
    noun: str,
) -> None:
    """Fail-closed schema/version/role validation on an opened database.

    ``noun`` only shapes diagnostics; ``expected_role`` is the enforcement.
    The candidate wording is preserved byte-for-byte from the phase-2
    contract so existing operator guidance and tests stay valid.
    """

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA synchronous = FULL")
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != APPLICATION_ID:
        raise ValueError(f"not a canonical {noun} database")
    if user_version > SCHEMA_VERSION:
        raise ValueError(
            f"{noun} schema version {user_version} is newer than "
            f"this build supports ({SCHEMA_VERSION}); upgrade "
            f"agentacct to open it"
        )
    if user_version < SCHEMA_VERSION:
        if read_only:
            raise ValueError(
                f"{noun} schema version {user_version} predates "
                f"this build ({SCHEMA_VERSION}); open writable to "
                f"upgrade, or rebuild the {noun}"
            )
        # Prove the role BEFORE migrating: a fail-closed open must not first
        # durably rewrite a store it is about to refuse (e.g. open_live handed
        # a candidate file). The role column exists at every migratable
        # version and never changes across steps.
        early_role = connection.execute(
            "SELECT store_role FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        if early_role is None or early_role["store_role"] != expected_role:
            raise ValueError(f"{noun} metadata is missing or incompatible")
        user_version = _apply_schema_migrations(
            connection,
            from_version=user_version,
            app_version=APP_VERSION_DEFAULT,
        )
    schema_sql = resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
    expected_checksum = hashlib.sha256(schema_sql.encode("utf-8")).hexdigest()
    migrations = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    actual_inventory = _schema_inventory(connection)
    expected_inventory = _expected_schema_inventory(schema_sql)
    versions = [int(row["version"]) for row in migrations]
    # One equality proves sorted, unique, contiguous, and ending
    # at this build's version — a deleted intermediate step row
    # must not pass as a verified chain.
    if not migrations or versions != list(
        range(versions[0], SCHEMA_VERSION + 1)
    ):
        raise ValueError(f"canonical {noun} schema migration metadata is incompatible")
    base = migrations[0]
    if int(base["version"]) == SCHEMA_VERSION:
        # Born on this build's schema: the base row must attest the
        # packaged schema.sql bytes exactly.
        if (
            base["name"] != SCHEMA_BASE_NAME
            or base["checksum"] != expected_checksum
        ):
            raise ValueError(f"canonical {noun} schema migration metadata is incompatible")
    else:
        # Migrated store: the birth row must attest the historical
        # schema.sql it was born with (pinned above for every superseded
        # checksum-pinned version), and each later step row must match the
        # registered step. The inventory comparison below is the structural
        # drift enforcement either way.
        historical = HISTORICAL_SCHEMA_CHECKSUMS.get(int(base["version"]))
        if historical is not None and (base["name"], base["checksum"]) != historical:
            raise ValueError(f"canonical {noun} schema migration metadata is incompatible")
        for row in migrations[1:]:
            step = SCHEMA_MIGRATIONS.get(int(row["version"]) - 1)
            if (
                step is None
                or row["name"] != step.name
                or row["checksum"] != step.checksum
            ):
                raise ValueError(f"canonical {noun} schema migration metadata is incompatible")
    if actual_inventory != expected_inventory:
        raise ValueError(f"canonical {noun} schema DDL is incompatible")
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if journal_mode != "wal":
        raise ValueError(f"canonical {noun} database is not in WAL mode")
    metadata = connection.execute(
        "SELECT store_role, schema_version FROM store_metadata WHERE singleton = 1"
    ).fetchone()
    if (
        metadata is None
        or metadata["store_role"] != expected_role
        or int(metadata["schema_version"]) != SCHEMA_VERSION
    ):
        raise ValueError(f"{noun} metadata is missing or incompatible")


def _bootstrap_new_store(
    connection: sqlite3.Connection,
    *,
    store_role: str,
    store_uuid: str | None,
    app_version: str,
    noun: str,
) -> None:
    """Initialize pragmas, schema, and metadata rows on a fresh database."""

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA synchronous = FULL")
    journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
    if journal_mode != "wal":
        raise RuntimeError(f"{noun} database did not enter WAL mode: {journal_mode}")
    schema_sql = resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
    schema_checksum = hashlib.sha256(schema_sql.encode("utf-8")).hexdigest()
    connection.executescript(schema_sql)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    timestamp = now_us()
    connection.execute(
        "INSERT INTO schema_migrations(version, name, checksum, app_version, applied_at_us) "
        "VALUES (?, ?, ?, ?, ?)",
        (SCHEMA_VERSION, SCHEMA_BASE_NAME, schema_checksum, app_version, timestamp),
    )
    connection.execute(
        "INSERT INTO store_metadata(singleton, store_uuid, store_role, schema_version, canonical_sequence, created_at_us) "
        "VALUES (1, ?, ?, ?, 0, ?)",
        (store_uuid or str(uuid.uuid4()), store_role, SCHEMA_VERSION, timestamp),
    )


def _device_inode(observed: os.stat_result) -> tuple[int, int]:
    return int(observed.st_dev), int(observed.st_ino)


def _require_owner_only_directory_stat(
    observed: os.stat_result,
    *,
    label: str,
) -> None:
    if not stat.S_ISDIR(observed.st_mode):
        raise ValueError(f"{label} must be a directory")
    if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) & 0o077:
        raise PermissionError(f"{label} must be owner-only")


def _prove_candidate_parent_paths(
    lexical_parent: Path,
    resolved_parent: Path,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """Re-prove both names still designate the one opened safe directory."""

    for parent in (lexical_parent, resolved_parent):
        _assert_no_symlink_components(parent, include_leaf=True)
        observed = parent.stat(follow_symlinks=False)
        _require_owner_only_directory_stat(
            observed,
            label="candidate database parent",
        )
        if _device_inode(observed) != expected_identity:
            raise RuntimeError("candidate database parent changed during validation")


def _prove_open_candidate_path(
    lexical_path: Path,
    resolved_path: Path,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """Re-prove one non-symlink, non-live candidate name and target."""

    _reject_live_store_shape(lexical_path)
    _reject_live_store_shape(resolved_path)
    _assert_no_symlink_components(lexical_path, include_leaf=True)
    if lexical_path.resolve(strict=True) != resolved_path:
        raise RuntimeError("candidate database path changed during validation")
    for observed in (
        lexical_path.stat(follow_symlinks=False),
        resolved_path.stat(follow_symlinks=False),
    ):
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or _device_inode(observed) != expected_identity
        ):
            raise RuntimeError("candidate database path changed during validation")
        if observed.st_uid != os.geteuid():
            raise PermissionError("candidate database must be owned by the current user")
        if stat.S_IMODE(observed.st_mode) != 0o600:
            raise PermissionError("candidate database must remain owner-only (0600)")


def _prove_candidate_sidecars(
    candidate: Path,
    *,
    parent_descriptor: int,
) -> None:
    """Reject unsafe existing WAL/SHM names before or after SQLite touches them."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for suffix in ("-wal", "-shm"):
        name = f"{candidate.name}{suffix}"
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(
                f"candidate SQLite sidecar cannot be opened safely: {name}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            named = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _device_inode(opened) != _device_inode(named)
            ):
                raise RuntimeError(
                    f"candidate SQLite sidecar path changed during validation: {name}"
                )
            if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600:
                raise PermissionError(
                    f"candidate SQLite sidecar must be owner-only (0600): {name}"
                )
        finally:
            os.close(descriptor)


class CanonicalStore:
    """One explicit candidate database and its owned SQLite connection."""

    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        *,
        read_only: bool,
        file_identity: tuple[int, int],
    ) -> None:
        self.path = path
        self.connection = connection
        self.read_only = read_only
        self._file_identity = file_identity
        self._savepoint_depth = 0
        self._transaction_depth = 0
        self.connection.row_factory = sqlite3.Row

    @property
    def file_identity(self) -> tuple[int, int]:
        """Device and inode proven for the SQLite main file at open time."""

        return self._file_identity

    @classmethod
    def create(
        cls,
        path: Path | str,
        *,
        store_uuid: str | None = None,
        app_version: str = APP_VERSION_DEFAULT,
    ) -> "CanonicalStore":
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
            raise RuntimeError(
                "canonical schema requires SQLite >= 3.37 for STRICT tables; "
                f"runtime is {sqlite3.sqlite_version}"
            )
        lexical_candidate = Path(path).expanduser()
        if not lexical_candidate.is_absolute():
            raise ValueError("candidate database path must be absolute")
        # Inspect the lexical shape first, then open and resolve the parent
        # exactly once.  Creation below is relative to that verified directory
        # descriptor, so a later parent-name exchange cannot redirect O_CREAT.
        _reject_live_store_shape(lexical_candidate, resolve_path=False)
        _assert_no_symlink_components(lexical_candidate, include_leaf=False)
        lexical_parent = lexical_candidate.parent
        parent_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            parent_descriptor = os.open(lexical_parent, parent_flags)
        except OSError as exc:
            raise ValueError("candidate database parent cannot be opened safely") from exc
        try:
            parent_stat = os.fstat(parent_descriptor)
            parent_identity = _device_inode(parent_stat)
            _require_owner_only_directory_stat(
                parent_stat,
                label="candidate database parent",
            )
            try:
                resolved_parent = lexical_parent.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError("candidate database parent cannot be resolved safely") from exc
            candidate = resolved_parent / lexical_candidate.name
            _reject_live_store_shape(candidate, resolve_path=False)
            _prove_candidate_parent_paths(
                lexical_parent,
                resolved_parent,
                expected_identity=parent_identity,
            )

            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(
                lexical_candidate.name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            reserved = os.fstat(descriptor)
            reserved_identity = _device_inode(reserved)
            connection: sqlite3.Connection | None = None
            try:
                _prove_candidate_parent_paths(
                    lexical_parent,
                    resolved_parent,
                    expected_identity=parent_identity,
                )
                # mode=rw prevents SQLite from creating a second file if the
                # validated name disappears before it opens the reserved inode.
                connection = sqlite3.connect(
                    f"{candidate.as_uri()}?mode=rw",
                    uri=True,
                    timeout=30,
                    isolation_level=None,
                )
                _prove_candidate_parent_paths(
                    lexical_parent,
                    resolved_parent,
                    expected_identity=parent_identity,
                )
                current = candidate.stat(follow_symlinks=False)
                if _device_inode(current) != reserved_identity:
                    raise RuntimeError("candidate database path changed before SQLite opened it")
                _bootstrap_new_store(
                    connection,
                    store_role="candidate",
                    store_uuid=store_uuid,
                    app_version=app_version,
                    noun="candidate",
                )

                # Keep the exclusive reservation descriptor until initialization is
                # complete.  Permission changes through the descriptor cannot
                # follow a swapped path or symlink.
                os.fchmod(descriptor, 0o600)
                reserved_final = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(reserved_final.st_mode)
                    or reserved_final.st_nlink != 1
                    or _device_inode(reserved_final) != reserved_identity
                ):
                    raise RuntimeError("reserved candidate database identity changed during setup")
                if reserved_final.st_uid != os.geteuid():
                    raise PermissionError("reserved candidate database is not owned by the current user")
                if stat.S_IMODE(reserved_final.st_mode) != 0o600:
                    raise PermissionError("reserved candidate database is not owner-only (0600)")

                _prove_candidate_parent_paths(
                    lexical_parent,
                    resolved_parent,
                    expected_identity=parent_identity,
                )
                current = candidate.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or _device_inode(current) != reserved_identity
                ):
                    raise RuntimeError("candidate database path changed during setup")
                if current.st_uid != os.geteuid():
                    raise PermissionError("candidate database must be owned by the current user")
                if stat.S_IMODE(current.st_mode) != 0o600:
                    raise PermissionError("candidate database must remain owner-only (0600)")

                sidecar_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                for sidecar in (Path(f"{candidate}-wal"), Path(f"{candidate}-shm")):
                    try:
                        sidecar_descriptor = os.open(sidecar, sidecar_flags)
                    except FileNotFoundError:
                        continue
                    try:
                        before = os.fstat(sidecar_descriptor)
                        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                            raise PermissionError(
                                f"candidate SQLite file is not unique and regular: {sidecar.name}"
                            )
                        sidecar_identity = _device_inode(before)
                        os.fchmod(sidecar_descriptor, 0o600)
                        after = os.fstat(sidecar_descriptor)
                        if (
                            not stat.S_ISREG(after.st_mode)
                            or after.st_nlink != 1
                            or _device_inode(after) != sidecar_identity
                            or stat.S_IMODE(after.st_mode) != 0o600
                        ):
                            raise PermissionError(
                                f"candidate SQLite file is not owner-only: {sidecar.name}"
                            )
                        path_stat = sidecar.stat(follow_symlinks=False)
                        if (
                            not stat.S_ISREG(path_stat.st_mode)
                            or path_stat.st_nlink != 1
                            or _device_inode(path_stat) != sidecar_identity
                            or stat.S_IMODE(path_stat.st_mode) != 0o600
                        ):
                            raise RuntimeError(
                                f"candidate SQLite file path changed during setup: {sidecar.name}"
                            )
                    finally:
                        os.close(sidecar_descriptor)

                # Last path-based proof before returning the connection.  The
                # descriptor remains open until the return expression completes.
                _prove_candidate_parent_paths(
                    lexical_parent,
                    resolved_parent,
                    expected_identity=parent_identity,
                )
                accepted = candidate.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(accepted.st_mode)
                    or accepted.st_nlink != 1
                    or _device_inode(accepted) != reserved_identity
                ):
                    raise RuntimeError("candidate database path changed before create returned")
                if stat.S_IMODE(accepted.st_mode) != 0o600:
                    raise PermissionError("candidate database must remain owner-only (0600)")
                return cls(
                    candidate,
                    connection,
                    read_only=False,
                    file_identity=reserved_identity,
                )
            except BaseException:
                if connection is not None:
                    connection.close()
                # Never unlink by path during failure cleanup.  A concurrent rename
                # can change the name after any identity check.  The explicit
                # owner-only scratch boundary retains failed files for inspection.
                raise
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_descriptor)

    @classmethod
    def open(cls, path: Path | str, *, read_only: bool = False) -> "CanonicalStore":
        lexical_candidate = Path(path).expanduser()
        if not lexical_candidate.is_absolute():
            raise ValueError("candidate database path must be absolute")
        _reject_live_store_shape(lexical_candidate)
        _assert_no_symlink_components(lexical_candidate, include_leaf=True)
        lexical_parent = lexical_candidate.parent
        parent_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            parent_descriptor = os.open(lexical_parent, parent_flags)
        except OSError as exc:
            raise ValueError("candidate database parent cannot be opened safely") from exc
        try:
            parent_stat = os.fstat(parent_descriptor)
            _require_owner_only_directory_stat(
                parent_stat,
                label="candidate database parent",
            )
            parent_identity = _device_inode(parent_stat)
            resolved_parent = lexical_parent.resolve(strict=True)
            _prove_candidate_parent_paths(
                lexical_parent,
                resolved_parent,
                expected_identity=parent_identity,
            )
            candidate = lexical_candidate.resolve(strict=True)
            _reject_live_store_shape(candidate, resolve_path=False)
            if candidate.parent != resolved_parent:
                raise RuntimeError("candidate database path changed during validation")
            lexical_stat = lexical_candidate.stat(follow_symlinks=False)
            if not stat.S_ISREG(lexical_stat.st_mode) or lexical_stat.st_nlink != 1:
                raise ValueError("candidate database must be a regular non-symlink file")
            if lexical_stat.st_uid != os.geteuid():
                raise PermissionError("candidate database must be owned by the current user")
            if stat.S_IMODE(lexical_stat.st_mode) != 0o600:
                raise PermissionError("candidate database must be owner-only (0600)")
            expected_identity = _device_inode(lexical_stat)
            _prove_open_candidate_path(
                lexical_candidate,
                candidate,
                expected_identity=expected_identity,
            )
            _prove_candidate_sidecars(
                candidate,
                parent_descriptor=parent_descriptor,
            )
            if read_only:
                connection = sqlite3.connect(
                    f"{candidate.as_uri()}?mode=ro",
                    uri=True,
                    timeout=30,
                    isolation_level=None,
                )
            else:
                connection = sqlite3.connect(
                    candidate,
                    timeout=30,
                    isolation_level=None,
                )
            try:
                _prove_candidate_parent_paths(
                    lexical_parent,
                    resolved_parent,
                    expected_identity=parent_identity,
                )
                _prove_open_candidate_path(
                    lexical_candidate,
                    candidate,
                    expected_identity=expected_identity,
                )
                _prove_candidate_sidecars(
                    candidate,
                    parent_descriptor=parent_descriptor,
                )
                _verify_opened_store(
                    connection,
                    read_only=read_only,
                    expected_role="candidate",
                    noun="candidate",
                )
                _prove_candidate_parent_paths(
                    lexical_parent,
                    resolved_parent,
                    expected_identity=parent_identity,
                )
                _prove_open_candidate_path(
                    lexical_candidate,
                    candidate,
                    expected_identity=expected_identity,
                )
                _prove_candidate_sidecars(
                    candidate,
                    parent_descriptor=parent_descriptor,
                )
            except sqlite3.DatabaseError as exc:
                connection.close()
                raise ValueError("canonical candidate schema DDL is unreadable") from exc
            except BaseException:
                connection.close()
                raise
            return cls(
                candidate,
                connection,
                read_only=read_only,
                file_identity=expected_identity,
            )
        finally:
            os.close(parent_descriptor)

    @staticmethod
    def _live_store_path(store_root: Path | str) -> Path:
        root = Path(store_root).expanduser()
        if not root.is_absolute():
            raise ValueError("live store root must be an absolute path")
        if not root.is_dir():
            raise ValueError("live store root must be an existing directory")
        return root / LIVE_STORE_FILENAME

    @classmethod
    def create_live(
        cls,
        store_root: Path | str,
        *,
        store_uuid: str | None = None,
        app_version: str = APP_VERSION_DEFAULT,
    ) -> "CanonicalStore":
        """Create the live store inside a agentacct state root, crash-safely.

        The inverse world of :meth:`create`: the database lives beside
        ``events.jsonl`` under the exact reserved name (which the candidate
        API structurally refuses), so the two roles can never cross paths.
        The parent directory is deliberately NOT required to be owner-only —
        real state roots ship 0755 and the JSONL ledger beside this file has
        the same exposure; the database file itself is created 0600.

        The reserved name must never hold a torn store: open_live is
        fail-closed, so a partial file at that name would permanently brick
        the shadow lane with no self-heal. The full bootstrap therefore runs
        on a uniquely-named sibling, and only a complete, checkpointed
        database is hard-linked onto the reserved name (link fails atomically
        if a racing creator won — the loser opens the winner's store). A
        crash mid-bootstrap litters one dead ``.new-*`` sibling and blocks
        nothing.
        """

        if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
            raise RuntimeError(
                "canonical schema requires SQLite >= 3.37 for STRICT tables; "
                f"runtime is {sqlite3.sqlite_version}"
            )
        path = cls._live_store_path(store_root)
        if path.exists():
            raise FileExistsError(f"live store already exists: {path}")
        staging = path.parent / f".{path.name}.new-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(staging, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            connection = sqlite3.connect(
                f"{staging.as_uri()}?mode=rw",
                uri=True,
                timeout=30,
                isolation_level=None,
            )
            try:
                _bootstrap_new_store(
                    connection,
                    store_role="live",
                    store_uuid=store_uuid,
                    app_version=app_version,
                    noun="live store",
                )
                # Fold the WAL into the main file so the linked name is a
                # complete standalone database (sidecars stay staging-named).
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
            os.link(staging, path)
        finally:
            os.close(descriptor)
            # The staging name is unique to this call, so unlinking it by
            # path cannot race another creator; on the success path the data
            # survives under the reserved name. The reserved name itself is
            # never unlinked here — on a lost race it is the winner's store.
            for leftover in (staging, Path(f"{staging}-wal"), Path(f"{staging}-shm")):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
        return cls.open_live(store_root)

    @classmethod
    def open_live(
        cls,
        store_root: Path | str,
        *,
        read_only: bool = False,
    ) -> "CanonicalStore":
        """Open the live store inside a agentacct state root, fail closed."""

        path = cls._live_store_path(store_root)
        observed = path.lstat()  # FileNotFoundError propagates for create-on-miss
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise ValueError("live store must be a regular non-symlink file")
        if observed.st_uid != os.geteuid():
            raise PermissionError("live store must be owned by the current user")
        if stat.S_IMODE(observed.st_mode) != 0o600:
            raise PermissionError("live store must be owner-only (0600)")
        expected_identity = _device_inode(observed)
        if read_only:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro",
                uri=True,
                timeout=30,
                isolation_level=None,
            )
        else:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=rw",
                uri=True,
                timeout=30,
                isolation_level=None,
            )
        try:
            _verify_opened_store(
                connection,
                read_only=read_only,
                expected_role="live",
                noun="live store",
            )
            current = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or _device_inode(current) != expected_identity
            ):
                raise RuntimeError("live store path changed during validation")
        except sqlite3.DatabaseError as exc:
            connection.close()
            raise ValueError("canonical live store schema DDL is unreadable") from exc
        except BaseException:
            connection.close()
            raise
        return cls(
            path,
            connection,
            read_only=read_only,
            file_identity=expected_identity,
        )

    @classmethod
    def open_or_create_live(cls, store_root: Path | str) -> "CanonicalStore":
        """Open the live store, creating it on first use.

        Two processes racing the first write are settled by O_EXCL: the
        loser's create fails and it opens the winner's store instead.
        """

        try:
            return cls.open_live(store_root)
        except FileNotFoundError:
            pass
        try:
            return cls.create_live(store_root)
        except FileExistsError:
            return cls.open_live(store_root)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CanonicalStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write and self.read_only:
            raise PermissionError("candidate database is open read-only")
        if self.connection.in_transaction:
            # Nested scopes become savepoints so an enclosing transaction can
            # make a multi-write operation atomic end to end (one aborted
            # legacy import must not leave a partially-committed candidate).
            name = f"canonical_scope_{self._savepoint_depth}"
            self._savepoint_depth += 1
            self._transaction_depth += 1
            try:
                self.connection.execute(f"SAVEPOINT {name}")
                try:
                    yield self.connection
                    self.connection.execute(f"RELEASE SAVEPOINT {name}")
                except BaseException:
                    if self.connection.in_transaction:
                        try:
                            self.connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
                            self.connection.execute(f"RELEASE SAVEPOINT {name}")
                        except sqlite3.Error:
                            pass
                    raise
            finally:
                self._savepoint_depth -= 1
                self._transaction_depth -= 1
            return
        if self._transaction_depth:
            # An enclosing transaction() scope is lexically active but its
            # SQLite transaction is gone (an auto-rollback whose error was
            # swallowed); beginning anew would commit durably inside a scope
            # the caller believes is atomic.
            raise RuntimeError(
                "enclosing transaction scope lost its SQLite transaction; "
                "the atomic scope cannot continue"
            )
        self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        self._transaction_depth += 1
        try:
            yield self.connection
            self.connection.execute("COMMIT")
        except BaseException:
            # SQLITE_FULL/IOERR/NOMEM can auto-roll-back the transaction; a bare
            # ROLLBACK would then raise and displace the root-cause exception.
            if self.connection.in_transaction:
                try:
                    self.connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            self._transaction_depth -= 1

    def repository(self) -> "CanonicalRepository":
        return CanonicalRepository(self)

    def quick_check(self) -> dict[str, object]:
        quick = [str(row[0]) for row in self.connection.execute("PRAGMA quick_check")]
        foreign = [tuple(row) for row in self.connection.execute("PRAGMA foreign_key_check")]
        return {"quick_check": quick, "foreign_key_violations": foreign, "ok": quick == ["ok"] and not foreign}

    def file_bytes(self) -> int:
        total = 0
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
        return total


class CanonicalRepository:
    """SQLite-backed implementation of the narrow spike repositories."""

    def __init__(self, store: CanonicalStore) -> None:
        self.store = store

    @property
    def connection(self) -> sqlite3.Connection:
        return self.store.connection

    def canonical_sequence(self, connection: sqlite3.Connection | None = None) -> int:
        con = connection or self.connection
        row = con.execute("SELECT canonical_sequence FROM store_metadata WHERE singleton = 1").fetchone()
        if row is None:
            raise RuntimeError("candidate store metadata is missing")
        return int(row[0])

    def _advance_sequence(self, connection: sqlite3.Connection) -> int:
        connection.execute(
            "UPDATE store_metadata SET canonical_sequence = canonical_sequence + 1 WHERE singleton = 1"
        )
        return self.canonical_sequence(connection)

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> SourceInstance:
        return SourceInstance(
            source_instance_id=int(row["source_instance_id"]),
            client=str(row["client"]),
            adapter=str(row["adapter"]),
            representation=str(row["representation"]),
            namespace_scheme=str(row["namespace_scheme"]),
            namespace_digest=bytes(row["namespace_digest"]),
            source_schema_version=row["source_schema_version"],
            privacy_label=row["privacy_label"],
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> Session:
        return Session(
            session_id=int(row["session_id"]),
            source_instance_id=int(row["source_instance_id"]),
            client_session_id=str(row["client_session_id"]),
            session_kind=str(row["session_kind"]),
            started_at_us=row["started_at_us"],
            last_activity_at_us=row["last_activity_at_us"],
            sort_activity_at_us=int(row["sort_activity_at_us"]),
            observation_order=row["observation_order"],
            observation_hash=bytes(row["observation_hash"]) if row["observation_hash"] is not None else None,
        )

    def get_source(
        self,
        client: str,
        representation: str,
        namespace_digest: bytes,
    ) -> SourceInstance | None:
        """Look up an existing source without creating migration identity."""

        if not client or not representation:
            raise ValueError("client and representation are required")
        if not isinstance(namespace_digest, bytes) or len(namespace_digest) != 32:
            raise ValueError("namespace_digest must contain exactly 32 bytes")
        row = self.connection.execute(
            "SELECT * FROM source_instances WHERE client = ? "
            "AND representation = ? AND namespace_digest = ?",
            (client, representation, namespace_digest),
        ).fetchone()
        return self._source_from_row(row) if row is not None else None

    def get_or_create_source(self, value: SourceInstanceInput) -> SourceInstance:
        with self.store.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM source_instances WHERE client = ? AND namespace_digest = ? AND representation = ?",
                (value.client, value.namespace_digest, value.representation),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    "INSERT INTO source_instances(client, adapter, representation, namespace_scheme, namespace_digest, "
                    "source_schema_version, privacy_label, created_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        value.client,
                        value.adapter,
                        value.representation,
                        value.namespace_scheme,
                        value.namespace_digest,
                        value.source_schema_version,
                        value.privacy_label,
                        now_us(),
                    ),
                )
                self._advance_sequence(connection)
                row = connection.execute(
                    "SELECT * FROM source_instances WHERE source_instance_id = ?", (int(cursor.lastrowid),)
                ).fetchone()
            assert row is not None
            if str(row["adapter"]) != value.adapter or str(row["namespace_scheme"]) != value.namespace_scheme:
                raise ValueError(
                    "source identity already exists with a different adapter or namespace scheme"
                )
            return self._source_from_row(row)

    def _record_conflict(
        self,
        connection: sqlite3.Connection,
        *,
        source_instance_id: int,
        native_entity_kind: str,
        native_entity_key: str,
        incumbent_hash: bytes,
        incoming_hash: bytes,
        reason: str,
    ) -> tuple[int, int]:
        existing = connection.execute(
            "SELECT source_conflict_id FROM source_conflicts WHERE source_instance_id = ? "
            "AND native_entity_kind = ? AND native_entity_key = ? AND incumbent_hash = ? "
            "AND incoming_hash = ? AND reason = ?",
            (
                source_instance_id,
                native_entity_kind,
                native_entity_key,
                incumbent_hash,
                incoming_hash,
                reason,
            ),
        ).fetchone()
        if existing is not None:
            return int(existing[0]), self.canonical_sequence(connection)
        timestamp = now_us()
        cursor = connection.execute(
            "INSERT INTO source_conflicts(source_instance_id, native_entity_kind, native_entity_key, "
            "incumbent_hash, incoming_hash, reason, first_seen_at_us, last_seen_at_us, seen_count, resolution_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'open')",
            (
                source_instance_id,
                native_entity_kind,
                native_entity_key,
                incumbent_hash,
                incoming_hash,
                reason,
                timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid), self._advance_sequence(connection)

    @staticmethod
    def _session_hash(value: SessionInput) -> bytes:
        return value.observation_hash or canonical_hash(
            {
                "source_instance_id": value.source_instance_id,
                "client_session_id": value.client_session_id,
                "session_kind": value.session_kind,
                "started_at_us": value.started_at_us,
                "last_activity_at_us": value.last_activity_at_us,
                "parent_client_session_id": value.parent_client_session_id,
                "parent_namespace_digest": value.parent_namespace_digest,
            }
        )

    @staticmethod
    def _store_observed_lineage(
        connection: sqlite3.Connection,
        *,
        session_id: int,
        value: SessionInput,
        timestamp: int,
    ) -> None:
        """Persist the last-wins raw parent claim alongside the session row.

        Absorb semantics never CLEAR a parent — an observation without one
        leaves the incumbent claim in place — so this only ever inserts or
        replaces, inside the caller's transaction.
        """

        if value.parent_client_session_id is None or value.parent_namespace_digest is None:
            return
        connection.execute(
            "INSERT INTO session_observed_lineage(session_id, parent_client, "
            "parent_client_session_id, parent_namespace_digest, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "parent_client = excluded.parent_client, "
            "parent_client_session_id = excluded.parent_client_session_id, "
            "parent_namespace_digest = excluded.parent_namespace_digest, "
            "updated_at_us = excluded.updated_at_us",
            (
                session_id,
                value.parent_client,
                value.parent_client_session_id,
                value.parent_namespace_digest,
                timestamp,
            ),
        )

    def reconcile_session(self, value: SessionInput) -> WriteResult:
        incoming_hash = self._session_hash(value)
        timestamp = now_us()
        with self.store.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM sessions WHERE source_instance_id = ? AND client_session_id = ?",
                (value.source_instance_id, value.client_session_id),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    "INSERT INTO sessions(source_instance_id, client_session_id, session_kind, started_at_us, "
                    "last_activity_at_us, sort_activity_at_us, observation_order, observation_hash, source_revision_id, "
                    "created_at_us, updated_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        value.source_instance_id,
                        value.client_session_id,
                        value.session_kind,
                        value.started_at_us,
                        value.last_activity_at_us,
                        value.last_activity_at_us or 0,
                        value.observation_order,
                        incoming_hash,
                        value.source_revision_id,
                        timestamp,
                        timestamp,
                    ),
                )
                session_id = int(cursor.lastrowid)
                self._store_observed_lineage(
                    connection, session_id=session_id, value=value, timestamp=timestamp
                )
                return WriteResult("inserted", session_id, self._advance_sequence(connection))
            row_id = int(existing["session_id"])
            incumbent_hash = bytes(existing["observation_hash"])
            if incumbent_hash == incoming_hash:
                incumbent_order = existing["observation_order"]
                advances_high_watermark = value.observation_order is not None and (
                    incumbent_order is None
                    or int(value.observation_order) > int(incumbent_order)
                )
                if advances_high_watermark:
                    # Semantic content is unchanged, so canonical sequence and
                    # projections do not move. Persisting the source-order
                    # high-watermark is nevertheless required to reject a
                    # later divergent observation that is stale relative to
                    # this exact-content poll, including after restart.
                    connection.execute(
                        "UPDATE sessions SET observation_order = ?, "
                        "source_revision_id = COALESCE(?, source_revision_id), "
                        "updated_at_us = ? WHERE session_id = ?",
                        (
                            value.observation_order,
                            value.source_revision_id,
                            timestamp,
                            row_id,
                        ),
                    )
                return WriteResult("noop", row_id, self.canonical_sequence(connection))
            incumbent_order = existing["observation_order"]
            ordered_newer = value.observation_order is not None and (
                incumbent_order is None or int(value.observation_order) > int(incumbent_order)
            )
            if not ordered_newer:
                conflict_id, sequence = self._record_conflict(
                    connection,
                    source_instance_id=value.source_instance_id,
                    native_entity_kind="session",
                    native_entity_key=value.client_session_id,
                    incumbent_hash=incumbent_hash,
                    incoming_hash=incoming_hash,
                    reason="unordered_content_change",
                )
                return WriteResult("conflict", row_id, sequence, conflict_id)
            connection.execute(
                "UPDATE sessions SET session_kind = ?, started_at_us = ?, last_activity_at_us = ?, "
                "sort_activity_at_us = ?, observation_order = ?, observation_hash = ?, source_revision_id = ?, "
                "updated_at_us = ? WHERE session_id = ?",
                (
                    value.session_kind,
                    value.started_at_us,
                    value.last_activity_at_us,
                    value.last_activity_at_us or 0,
                    value.observation_order,
                    incoming_hash,
                    value.source_revision_id,
                    timestamp,
                    row_id,
                ),
            )
            self._store_observed_lineage(
                connection, session_id=row_id, value=value, timestamp=timestamp
            )
            return WriteResult("updated", row_id, self._advance_sequence(connection))

    def absorb_session_observation(self, value: SessionInput) -> WriteResult:
        """Fold an observation's ORDER-FREE fields (min started_at, max
        last_activity) into an existing session row, without advancing the
        source-order high-watermark and without recording a conflict.

        This is the importer's whole-file absorb expressed incrementally. min()
        and max() are commutative, associative, and idempotent, so folding a
        non-newer arrival still converges on the exact started_at/last_activity
        the importer's one-shot collapse produces — regardless of arrival order.
        The last-wins fields (session_kind, the parent lineage, observation_order)
        are deliberately NOT touched: on a non-newer arrival the incumbent is the
        newest observation, so the importer's last-wins pass keeps those too.

        The merge is computed here from the incumbent and the incoming value, so
        it is inherently monotonic-safe (it can only lower started_at and raise
        last_activity) even if a caller passes an ill-formed value, and the
        observation_hash is recomputed from the incumbent's own last-wins fields
        rather than trusted from the caller. Requires an existing row — the
        live writer only calls this when it has already read the incumbent."""

        timestamp = now_us()
        with self.store.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM sessions WHERE source_instance_id = ? AND client_session_id = ?",
                (value.source_instance_id, value.client_session_id),
            ).fetchone()
            if existing is None:
                raise ValueError(
                    "absorb_session_observation requires an existing session row"
                )
            row_id = int(existing["session_id"])
            cur_started = existing["started_at_us"]
            cur_last = existing["last_activity_at_us"]
            new_started = cur_started
            if value.started_at_us is not None and (
                cur_started is None or int(value.started_at_us) < int(cur_started)
            ):
                new_started = int(value.started_at_us)
            new_last = cur_last
            if value.last_activity_at_us is not None and (
                cur_last is None or int(value.last_activity_at_us) > int(cur_last)
            ):
                new_last = int(value.last_activity_at_us)
            if new_started == cur_started and new_last == cur_last:
                # Nothing order-free to fold in; the row already dominates.
                return WriteResult("noop", row_id, self.canonical_sequence(connection))
            lineage = connection.execute(
                "SELECT parent_client_session_id, parent_namespace_digest "
                "FROM session_observed_lineage WHERE session_id = ?",
                (row_id,),
            ).fetchone()
            merged_hash = canonical_hash(
                session_observation_hash_material(
                    client_session_id=str(existing["client_session_id"]),
                    session_kind=str(existing["session_kind"]),
                    started_at_us=new_started,
                    last_activity_at_us=new_last,
                    parent_client_session_id=(
                        str(lineage["parent_client_session_id"])
                        if lineage is not None
                        and lineage["parent_client_session_id"] is not None
                        else None
                    ),
                    parent_namespace_digest=(
                        bytes(lineage["parent_namespace_digest"])
                        if lineage is not None
                        and lineage["parent_namespace_digest"] is not None
                        else None
                    ),
                )
            )
            connection.execute(
                "UPDATE sessions SET started_at_us = ?, last_activity_at_us = ?, "
                "sort_activity_at_us = ?, observation_hash = ?, updated_at_us = ? "
                "WHERE session_id = ?",
                (new_started, new_last, new_last or 0, merged_hash, timestamp, row_id),
            )
            return WriteResult("absorbed", row_id, self._advance_sequence(connection))

    def get_sessions(self, keys: Sequence[tuple[int, str]]) -> Mapping[tuple[int, str], Session]:
        if not keys:
            return {}
        placeholders = ",".join("(?, ?)" for _ in keys)
        params: list[object] = []
        for source_instance_id, client_session_id in keys:
            params.extend((source_instance_id, client_session_id))
        rows = self.connection.execute(
            f"SELECT * FROM sessions WHERE (source_instance_id, client_session_id) IN ({placeholders})", params
        ).fetchall()
        return {
            (int(row["source_instance_id"]), str(row["client_session_id"])): self._session_from_row(row)
            for row in rows
        }

    def get_session(self, source_instance_id: int, client_session_id: str) -> Session | None:
        return self.get_sessions([(source_instance_id, client_session_id)]).get(
            (source_instance_id, client_session_id)
        )

    @staticmethod
    def _retire_task_anchor_in_transaction(
        connection: sqlite3.Connection,
        *,
        child_session_id: int,
    ) -> bool:
        deleted = connection.execute(
            "DELETE FROM task_anchors WHERE primary_session_id = ?",
            (child_session_id,),
        )
        return deleted.rowcount == 1

    def add_session_edge(self, value: SessionEdgeInput) -> WriteResult:
        with self.store.transaction(write=True) as connection:
            endpoints = connection.execute(
                "SELECT s.session_id, s.source_instance_id, si.client, si.namespace_scheme, si.namespace_digest "
                "FROM sessions s JOIN source_instances si USING(source_instance_id) "
                "WHERE s.session_id IN (?, ?)",
                (value.child_session_id, value.parent_session_id),
            ).fetchall()
            if len(endpoints) != 2:
                raise ValueError("both session-edge endpoints must exist")
            by_id = {int(row["session_id"]): row for row in endpoints}
            child = by_id[value.child_session_id]
            parent = by_id[value.parent_session_id]
            compatible = (
                child["client"] == parent["client"]
                and child["namespace_scheme"] == parent["namespace_scheme"]
                and bytes(child["namespace_digest"]) == bytes(parent["namespace_digest"])
            )
            validation_state = value.validation_state if compatible else "rejected"
            veto_reason = None if compatible else "incompatible_source_namespace"
            existing = connection.execute(
                "SELECT session_edge_id, content_hash, validation_state, veto_reason FROM session_edges "
                "WHERE child_session_id = ? AND parent_session_id = ? AND relation = ? AND basis = ?",
                (value.child_session_id, value.parent_session_id, value.relation, value.basis),
            ).fetchone()
            if existing is not None:
                if (
                    bytes(existing["content_hash"]) == value.content_hash
                    and existing["validation_state"] == validation_state
                    and existing["veto_reason"] == veto_reason
                ):
                    if validation_state == "valid" and self._retire_task_anchor_in_transaction(
                        connection,
                        child_session_id=value.child_session_id,
                    ):
                        return WriteResult(
                            "updated",
                            int(existing["session_edge_id"]),
                            self._advance_sequence(connection),
                        )
                    return WriteResult("noop", int(existing["session_edge_id"]), self.canonical_sequence(connection))
                conflict_id, sequence = self._record_conflict(
                    connection,
                    source_instance_id=int(child["source_instance_id"]),
                    native_entity_kind="session_edge",
                    native_entity_key=f"{value.child_session_id}:{value.parent_session_id}:{value.relation}:{value.basis}",
                    incumbent_hash=bytes(existing["content_hash"]),
                    incoming_hash=value.content_hash,
                    reason="edge_content_change",
                )
                return WriteResult("conflict", int(existing["session_edge_id"]), sequence, conflict_id)
            if validation_state == "valid":
                other_parent = connection.execute(
                    "SELECT session_edge_id FROM session_edges "
                    "WHERE child_session_id = ? AND validation_state = 'valid' LIMIT 1",
                    (value.child_session_id,),
                ).fetchone()
                if other_parent is not None:
                    validation_state = "rejected"
                    veto_reason = "ambiguous_multiple_parent"
                else:
                    creates_cycle = connection.execute(
                        "WITH RECURSIVE ancestors(session_id) AS ("
                        "SELECT parent_session_id FROM session_edges "
                        "WHERE child_session_id = ? AND validation_state = 'valid' "
                        "UNION SELECT edge.parent_session_id FROM session_edges edge "
                        "JOIN ancestors ON edge.child_session_id = ancestors.session_id "
                        "WHERE edge.validation_state = 'valid'"
                        ") SELECT 1 FROM ancestors WHERE session_id = ? LIMIT 1",
                        (value.parent_session_id, value.child_session_id),
                    ).fetchone()
                    if creates_cycle is not None:
                        validation_state = "rejected"
                        veto_reason = "lineage_cycle"
            cursor = connection.execute(
                "INSERT INTO session_edges(child_session_id, parent_session_id, relation, basis, validation_state, "
                "veto_reason, source_revision_id, content_hash, created_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    value.child_session_id,
                    value.parent_session_id,
                    value.relation,
                    value.basis,
                    validation_state,
                    veto_reason,
                    value.source_revision_id,
                    value.content_hash,
                    now_us(),
                ),
            )
            sequence = self._advance_sequence(connection)
            if validation_state == "valid" and self._retire_task_anchor_in_transaction(
                connection,
                child_session_id=value.child_session_id,
            ):
                sequence = self._advance_sequence(connection)
            return WriteResult("inserted", int(cursor.lastrowid), sequence)

    def reconcile_observed_parent_edge(
        self,
        *,
        child_session_id: int,
        parent_session_id: int,
        relation: str,
        basis: str,
        content_hash: bytes,
    ) -> WriteResult:
        """Converge the child's explicit-parent edge onto the newest absorb.

        Session edges are DERIVED graph state — every bulk import regenerates
        them wholesale from the final absorbed observation, so an incremental
        writer must supersede its own earlier derivations or a stale parent
        claim stays the one valid edge while the newest truth lands rejected
        as ``ambiguous_multiple_parent``. Any same-basis edge that no longer
        matches the newest (parent, relation, content) exactly is deleted
        inside this transaction, then the current derivation is (re)inserted
        through :meth:`add_session_edge`, which re-runs namespace, cycle, and
        anchor-retirement validation from scratch.
        """

        with self.store.transaction(write=True) as connection:
            stale = connection.execute(
                "SELECT session_edge_id FROM session_edges "
                "WHERE child_session_id = ? AND basis = ? "
                "AND NOT (parent_session_id = ? AND relation = ? AND content_hash = ?)",
                (child_session_id, basis, parent_session_id, relation, content_hash),
            ).fetchall()
            for row in stale:
                connection.execute(
                    "DELETE FROM session_edges WHERE session_edge_id = ?",
                    (int(row[0]),),
                )
            if stale:
                self._advance_sequence(connection)
            exact = connection.execute(
                "SELECT session_edge_id, validation_state FROM session_edges "
                "WHERE child_session_id = ? AND parent_session_id = ? "
                "AND relation = ? AND basis = ?",
                (child_session_id, parent_session_id, relation, basis),
            ).fetchone()
            if exact is not None:
                return WriteResult(
                    "noop", int(exact["session_edge_id"]), self.canonical_sequence(connection)
                )
            return self.add_session_edge(
                SessionEdgeInput(
                    child_session_id=child_session_id,
                    parent_session_id=parent_session_id,
                    relation=relation,  # type: ignore[arg-type]
                    basis=basis,
                    validation_state="valid",
                    content_hash=content_hash,
                )
            )

    def retire_task_anchor(self, session_id: int) -> bool:
        """Retire a session's task anchor outside the valid-edge path.

        The importer's anchor pass never anchors a session whose final
        absorbed observation claims a parent or a non-root kind; an
        incremental writer that anchored an EARLIER honest state must be able
        to converge when a newer observation withdraws the basis.
        """

        with self.store.transaction(write=True) as connection:
            if self._retire_task_anchor_in_transaction(
                connection, child_session_id=session_id
            ):
                self._advance_sequence(connection)
                return True
            return False

    @staticmethod
    def _task_anchor_from_row(row: sqlite3.Row) -> TaskAnchor:
        return TaskAnchor(
            task_anchor_id=int(row["task_anchor_id"]),
            public_task_id=str(row["public_task_id"]),
            primary_session_id=int(row["primary_session_id"]),
        )

    def get_or_create_task_anchor(self, primary_session_id: int) -> TaskAnchor:
        with self.store.transaction(write=True) as connection:
            session = connection.execute(
                "SELECT session_kind, EXISTS("
                "SELECT 1 FROM session_edges WHERE child_session_id = sessions.session_id "
                "AND validation_state = 'valid'"
                ") AS has_valid_parent FROM sessions WHERE session_id = ?",
                (primary_session_id,),
            ).fetchone()
            if session is None:
                raise ValueError("primary Task session does not exist")
            if session["has_valid_parent"]:
                raise ValueError("Task anchors require a root session without a valid parent")
            if session["session_kind"] not in {"root", "unknown"}:
                raise ValueError("Task anchors require a root session")
            row = connection.execute(
                "SELECT * FROM task_anchors WHERE primary_session_id = ?", (primary_session_id,)
            ).fetchone()
            if row is None:
                for _ in range(8):
                    public_task_id = TASK_PUBLIC_ID_PREFIX + secrets.token_hex(16)
                    try:
                        cursor = connection.execute(
                            "INSERT INTO task_anchors(public_task_id, primary_session_id, created_at_us) VALUES (?, ?, ?)",
                            (public_task_id, primary_session_id, now_us()),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    self._advance_sequence(connection)
                    row = connection.execute(
                        "SELECT * FROM task_anchors WHERE task_anchor_id = ?", (int(cursor.lastrowid),)
                    ).fetchone()
                    break
                else:
                    raise RuntimeError("failed to allocate a unique opaque Task ID")
            assert row is not None
            return self._task_anchor_from_row(row)

    def get_task_anchor(self, public_task_id: str) -> TaskAnchor | None:
        row = self.connection.execute(
            "SELECT * FROM task_anchors WHERE public_task_id = ?", (public_task_id,)
        ).fetchone()
        return self._task_anchor_from_row(row) if row is not None else None

    def find_fact_id(self, source_instance_id: int, source_event_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT fact_id FROM facts WHERE source_instance_id = ? AND source_event_id = ?",
            (source_instance_id, source_event_id),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def _append_fact_in_transaction(
        self, connection: sqlite3.Connection, value: FactInput
    ) -> WriteResult:
        identity_rows: list[sqlite3.Row] = []
        identity_key_parts: list[str] = []
        if value.source_event_id is not None:
            identity_key_parts.append(
                f"source:{value.source_instance_id}:{value.source_event_id}"
            )
            row = connection.execute(
                "SELECT fact_id, content_hash FROM facts "
                "WHERE source_instance_id = ? AND source_event_id = ?",
                (value.source_instance_id, value.source_event_id),
            ).fetchone()
            if row is not None:
                identity_rows.append(row)
        if value.idempotency_key is not None:
            identity_key_parts.append(
                "idempotency:"
                f"{value.source_instance_id}:{value.idempotency_scope}:{value.idempotency_key}"
            )
            row = connection.execute(
                "SELECT fact_id, content_hash FROM facts "
                "WHERE source_instance_id = ? AND idempotency_scope = ? AND idempotency_key = ?",
                (
                    value.source_instance_id,
                    value.idempotency_scope,
                    value.idempotency_key,
                ),
            ).fetchone()
            if row is not None:
                identity_rows.append(row)
        distinct_existing = {int(row["fact_id"]): row for row in identity_rows}
        identity_key = "|".join(identity_key_parts)
        if len(distinct_existing) > 1:
            incumbent = next(iter(distinct_existing.values()))
            conflict_id, sequence = self._record_conflict(
                connection,
                source_instance_id=value.source_instance_id,
                native_entity_kind="fact",
                native_entity_key=identity_key,
                incumbent_hash=bytes(incumbent["content_hash"]),
                incoming_hash=value.content_hash,
                reason="stable_identities_resolve_to_different_facts",
            )
            return WriteResult("conflict", int(incumbent["fact_id"]), sequence, conflict_id)
        existing = next(iter(distinct_existing.values()), None)
        if existing is not None:
            if bytes(existing["content_hash"]) == value.content_hash:
                return WriteResult("noop", int(existing["fact_id"]), self.canonical_sequence(connection))
            conflict_id, sequence = self._record_conflict(
                connection,
                source_instance_id=value.source_instance_id,
                native_entity_kind="fact",
                native_entity_key=identity_key,
                incumbent_hash=bytes(existing["content_hash"]),
                incoming_hash=value.content_hash,
                reason="stable_identity_content_change",
            )
            return WriteResult("conflict", int(existing["fact_id"]), sequence, conflict_id)
        if value.supersedes_fact_id is not None:
            superseded = connection.execute(
                "SELECT source_instance_id, fact_type FROM facts WHERE fact_id = ?",
                (value.supersedes_fact_id,),
            ).fetchone()
            if superseded is None or int(superseded["source_instance_id"]) != value.source_instance_id:
                raise ValueError("a correction may supersede only an existing fact in the same source namespace")
            if str(superseded["fact_type"]) != value.fact_type:
                raise ValueError("a correction must preserve the superseded fact type")
            successor = connection.execute(
                "SELECT fact_id, content_hash FROM facts WHERE supersedes_fact_id = ?",
                (value.supersedes_fact_id,),
            ).fetchone()
            if successor is not None:
                conflict_id, sequence = self._record_conflict(
                    connection,
                    source_instance_id=value.source_instance_id,
                    native_entity_kind="fact_correction",
                    native_entity_key=str(value.supersedes_fact_id),
                    incumbent_hash=bytes(successor["content_hash"]),
                    incoming_hash=value.content_hash,
                    reason="superseded_fact_already_has_correction",
                )
                return WriteResult("conflict", int(successor["fact_id"]), sequence, conflict_id)
        cursor = connection.execute(
            "INSERT INTO facts(source_instance_id, source_event_id, idempotency_scope, idempotency_key, fact_type, "
            "transport, strength, occurred_at_us, source_order, content_hash, supersedes_fact_id, created_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                value.source_instance_id,
                value.source_event_id,
                value.idempotency_scope,
                value.idempotency_key,
                value.fact_type,
                value.transport,
                value.strength,
                value.occurred_at_us,
                value.source_order,
                value.content_hash,
                value.supersedes_fact_id,
                now_us(),
            ),
        )
        fact_id = int(cursor.lastrowid)
        if value.supersedes_fact_id is not None:
            # A correction inherits every already-valid session scope from the
            # logical fact it supersedes.  This makes the correction safe at
            # the same transaction boundary as the immutable fact insert: a
            # caller cannot temporarily suppress the predecessor and then
            # attach its successor to an unrelated Task.
            connection.execute(
                "INSERT INTO fact_session_links(fact_id, session_id, method, confidence, rule_version, "
                "validation_state, veto_reason) "
                "SELECT ?, session_id, method, confidence, rule_version, validation_state, veto_reason "
                "FROM fact_session_links WHERE fact_id = ? AND validation_state = 'valid' "
                "AND confidence NOT IN ('ambiguous', 'rejected', 'conflict')",
                (fact_id, value.supersedes_fact_id),
            )
        return WriteResult("inserted", fact_id, self._advance_sequence(connection))

    def append_fact(self, value: FactInput) -> WriteResult:
        with self.store.transaction(write=True) as connection:
            return self._append_fact_in_transaction(connection, value)

    def append_work_claim(self, value: WorkClaimInput) -> WriteResult:
        with self.store.transaction(write=True) as connection:
            result = self._append_fact_in_transaction(connection, value.fact)
            if result.disposition != "inserted":
                return result
            assert result.row_id is not None
            connection.execute(
                "INSERT INTO work_claims(fact_id, event_kind, status, title, objective, summary, blocker, next_step, "
                "work_id, section_id, outcome_axis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.row_id,
                    value.event_kind,
                    value.status,
                    value.title,
                    value.objective,
                    value.summary,
                    value.blocker,
                    value.next_step,
                    value.work_id,
                    value.section_id,
                    value.outcome_axis,
                ),
            )
            return result

    @staticmethod
    def _work_claim_columns(value: WorkClaimInput) -> tuple[object, ...]:
        return (
            value.event_kind,
            value.status,
            value.title,
            value.objective,
            value.summary,
            value.blocker,
            value.next_step,
            value.work_id,
            value.section_id,
            value.outcome_axis,
        )

    @staticmethod
    def _fact_columns(value: FactInput) -> tuple[object, ...]:
        return (
            value.source_instance_id,
            value.source_event_id,
            value.idempotency_scope,
            value.idempotency_key,
            value.fact_type,
            value.transport,
            value.strength,
            value.occurred_at_us,
            value.source_order,
            value.content_hash,
            value.supersedes_fact_id,
        )

    def _assert_existing_work_claim(
        self,
        connection: sqlite3.Connection,
        *,
        fact_id: int,
        value: WorkClaimInput,
    ) -> None:
        row = connection.execute(
            "SELECT event_kind, status, title, objective, summary, blocker, next_step, "
            "work_id, section_id, outcome_axis FROM work_claims WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
        if row is None or tuple(row) != self._work_claim_columns(value):
            raise ValueError(
                "recovered fact identity exists without the identical work claim"
            )

    def _existing_fact_for_recovery(
        self,
        connection: sqlite3.Connection,
        value: FactInput,
    ) -> sqlite3.Row | None:
        rows: dict[int, sqlite3.Row] = {}
        if value.source_event_id is not None:
            row = connection.execute(
                "SELECT * FROM facts WHERE source_instance_id = ? AND source_event_id = ?",
                (value.source_instance_id, value.source_event_id),
            ).fetchone()
            if row is not None:
                rows[int(row["fact_id"])] = row
        if value.idempotency_key is not None:
            row = connection.execute(
                "SELECT * FROM facts WHERE source_instance_id = ? "
                "AND idempotency_scope = ? AND idempotency_key = ?",
                (
                    value.source_instance_id,
                    value.idempotency_scope,
                    value.idempotency_key,
                ),
            ).fetchone()
            if row is not None:
                rows[int(row["fact_id"])] = row
        if len(rows) > 1:
            raise ValueError(
                "recovered fact stable identities resolve to different canonical facts"
            )
        return next(iter(rows.values()), None)

    def _assert_existing_recovered_fact(
        self,
        row: sqlite3.Row,
        value: FactInput,
    ) -> None:
        observed = tuple(
            row[name]
            for name in (
                "source_instance_id",
                "source_event_id",
                "idempotency_scope",
                "idempotency_key",
                "fact_type",
                "transport",
                "strength",
                "occurred_at_us",
                "source_order",
                "content_hash",
                "supersedes_fact_id",
            )
        )
        if observed != self._fact_columns(value):
            raise ValueError(
                "recovered fact identity exists with different canonical content"
            )

    @staticmethod
    def _locked_recovery_link(
        *,
        fact_id: int,
        session_id: int,
    ) -> FactSessionLinkInput:
        return FactSessionLinkInput(
            fact_id=fact_id,
            session_id=session_id,
            method=RECOVERY_LINK_METHOD,
            confidence="high",
            rule_version=RECOVERY_LINK_RULE_VERSION,
            validation_state="valid",
        )

    def _assert_locked_recovery_link(
        self,
        connection: sqlite3.Connection,
        *,
        fact_id: int,
        session_id: int,
        message: str,
    ) -> int:
        rows = connection.execute(
            "SELECT fact_session_link_id, session_id, method, confidence, rule_version, "
            "validation_state, veto_reason FROM fact_session_links WHERE fact_id = ? "
            "ORDER BY fact_session_link_id",
            (fact_id,),
        ).fetchall()
        expected = (
            session_id,
            RECOVERY_LINK_METHOD,
            "high",
            RECOVERY_LINK_RULE_VERSION,
            "valid",
            None,
        )
        if len(rows) != 1 or tuple(rows[0])[1:] != expected:
            raise ValueError(message)
        return int(rows[0][0])

    @staticmethod
    def _recovery_identity_keys(value: RecoveredWorkClaimInput) -> tuple[tuple[object, ...], ...]:
        fact = value.claim.fact
        keys: list[tuple[object, ...]] = []
        if fact.source_event_id is not None:
            keys.append(("source_event", fact.source_instance_id, fact.source_event_id))
        if fact.idempotency_key is not None:
            keys.append(
                (
                    "idempotency",
                    fact.source_instance_id,
                    fact.idempotency_scope,
                    fact.idempotency_key,
                )
            )
        return tuple(keys)

    def _preflight_recovered_work_claims(
        self,
        connection: sqlite3.Connection,
        values: tuple[RecoveredWorkClaimInput, ...],
    ) -> None:
        planned_identities: dict[tuple[object, ...], RecoveredWorkClaimInput] = {}
        planned_successors: dict[int, RecoveredWorkClaimInput] = {}
        for value in values:
            if not isinstance(value, RecoveredWorkClaimInput):
                raise ValueError(
                    "append_recovered_work_claims requires RecoveredWorkClaimInput values"
                )
            fact = value.claim.fact
            if fact.transport != RECOVERY_FACT_TRANSPORT:
                raise ValueError("recovered work claims require the recovery transport")

            session = connection.execute(
                "SELECT source_instance_id FROM sessions WHERE session_id = ?",
                (value.session_id,),
            ).fetchone()
            if session is None:
                raise ValueError("recovered work claim session must already exist")
            if int(session[0]) != fact.source_instance_id:
                raise ValueError(
                    "recovered work claim source must exactly match its existing session source"
                )

            for identity in self._recovery_identity_keys(value):
                prior = planned_identities.get(identity)
                if prior is not None and prior != value:
                    raise ValueError(
                        "recovered batch contains conflicting stable fact identities"
                    )
                planned_identities[identity] = value

            existing = self._existing_fact_for_recovery(connection, fact)
            if existing is not None:
                self._assert_existing_recovered_fact(existing, fact)
                existing_fact_id = int(existing["fact_id"])
                self._assert_existing_work_claim(
                    connection,
                    fact_id=existing_fact_id,
                    value=value.claim,
                )
                self._assert_locked_recovery_link(
                    connection,
                    fact_id=existing_fact_id,
                    session_id=value.session_id,
                    message=(
                        "recovered fact identity exists without the identical "
                        "high-confidence link"
                    ),
                )

            predecessor_id = fact.supersedes_fact_id
            if predecessor_id is None:
                continue
            predecessor = connection.execute(
                "SELECT source_instance_id, fact_type, transport FROM facts WHERE fact_id = ?",
                (predecessor_id,),
            ).fetchone()
            if (
                predecessor is None
                or int(predecessor["source_instance_id"]) != fact.source_instance_id
                or str(predecessor["fact_type"]) != "work_claim"
                or str(predecessor["transport"]) != RECOVERY_FACT_TRANSPORT
                or connection.execute(
                    "SELECT 1 FROM work_claims WHERE fact_id = ?",
                    (predecessor_id,),
                ).fetchone()
                is None
            ):
                raise ValueError(
                    "recovered correction may supersede only the same high-confidence recovery scope"
                )
            self._assert_locked_recovery_link(
                connection,
                fact_id=predecessor_id,
                session_id=value.session_id,
                message=(
                    "recovered correction may supersede only the same "
                    "high-confidence recovery scope"
                ),
            )

            prior_successor = planned_successors.get(predecessor_id)
            if prior_successor is not None and prior_successor != value:
                raise ValueError(
                    "recovered batch contains competing corrections for one fact"
                )
            planned_successors[predecessor_id] = value
            successor = connection.execute(
                "SELECT fact_id FROM facts WHERE supersedes_fact_id = ?",
                (predecessor_id,),
            ).fetchone()
            if successor is not None and (
                existing is None or int(successor[0]) != int(existing["fact_id"])
            ):
                raise ValueError(
                    "recovered correction predecessor already has a different successor"
                )

    def _link_fact_to_session_in_transaction(
        self,
        connection: sqlite3.Connection,
        value: FactSessionLinkInput,
    ) -> WriteResult:
        endpoints = connection.execute(
            "SELECT fact.fact_id, fact.source_instance_id AS fact_source_id, "
            "session.session_id, session.source_instance_id AS session_source_id, "
            "fact_source.client AS fact_client, fact_source.namespace_scheme AS fact_scheme, "
            "fact_source.namespace_digest AS fact_digest, session_source.client AS session_client, "
            "session_source.namespace_scheme AS session_scheme, session_source.namespace_digest AS session_digest "
            "FROM facts fact CROSS JOIN sessions session "
            "JOIN source_instances fact_source ON fact_source.source_instance_id = fact.source_instance_id "
            "JOIN source_instances session_source ON session_source.source_instance_id = session.source_instance_id "
            "WHERE fact.fact_id = ? AND session.session_id = ?",
            (value.fact_id, value.session_id),
        ).fetchone()
        if endpoints is None:
            raise ValueError("fact-session link endpoints must exist")
        compatible = (
            endpoints["fact_client"] == endpoints["session_client"]
            and endpoints["fact_scheme"] == endpoints["session_scheme"]
            and bytes(endpoints["fact_digest"]) == bytes(endpoints["session_digest"])
        )
        validation_state = value.validation_state if compatible else "rejected"
        veto_reason = value.veto_reason if compatible else "incompatible_source_namespace"
        superseded = connection.execute(
            "SELECT supersedes_fact_id FROM facts WHERE fact_id = ?",
            (value.fact_id,),
        ).fetchone()
        if (
            compatible
            and superseded is not None
            and superseded["supersedes_fact_id"] is not None
        ):
            predecessor_scope = connection.execute(
                "SELECT 1 FROM fact_session_links WHERE fact_id = ? AND session_id = ? "
                "AND validation_state = 'valid' "
                "AND confidence NOT IN ('ambiguous', 'rejected', 'conflict') LIMIT 1",
                (int(superseded["supersedes_fact_id"]), value.session_id),
            ).fetchone()
            if predecessor_scope is None:
                validation_state = "rejected"
                veto_reason = "correction_session_scope_mismatch"
        existing = connection.execute(
            "SELECT fact_session_link_id, confidence, validation_state, veto_reason "
            "FROM fact_session_links WHERE fact_id = ? AND session_id = ? AND method = ? AND rule_version = ?",
            (value.fact_id, value.session_id, value.method, value.rule_version),
        ).fetchone()
        if existing is not None:
            if (
                existing["confidence"] == value.confidence
                and existing["validation_state"] == validation_state
                and existing["veto_reason"] == veto_reason
            ):
                return WriteResult(
                    "noop",
                    int(existing["fact_session_link_id"]),
                    self.canonical_sequence(connection),
                )
            conflict_id, sequence = self._record_conflict(
                connection,
                source_instance_id=int(endpoints["fact_source_id"]),
                native_entity_kind="fact_session_link",
                native_entity_key=f"{value.fact_id}:{value.session_id}:{value.method}:{value.rule_version}",
                incumbent_hash=canonical_hash(dict(existing)),
                incoming_hash=canonical_hash(dataclass_dict(value)),
                reason="link_content_change",
            )
            return WriteResult(
                "conflict",
                int(existing["fact_session_link_id"]),
                sequence,
                conflict_id,
            )
        cursor = connection.execute(
            "INSERT INTO fact_session_links(fact_id, session_id, method, confidence, rule_version, "
            "validation_state, veto_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                value.fact_id,
                value.session_id,
                value.method,
                value.confidence,
                value.rule_version,
                validation_state,
                veto_reason,
            ),
        )
        return WriteResult("inserted", int(cursor.lastrowid), self._advance_sequence(connection))

    def link_fact_to_session(self, value: FactSessionLinkInput) -> WriteResult:
        with self.store.transaction(write=True) as connection:
            return self._link_fact_to_session_in_transaction(connection, value)

    def _append_recovered_work_claim_in_transaction(
        self,
        connection: sqlite3.Connection,
        value: RecoveredWorkClaimInput,
    ) -> tuple[WriteResult, WriteResult]:
        claim = value.claim
        result = self._append_fact_in_transaction(connection, claim.fact)
        if result.disposition == "conflict" or result.row_id is None:
            raise ValueError("recovered work claim conflicts with canonical fact identity")

        if result.disposition == "noop":
            existing = self._existing_fact_for_recovery(connection, claim.fact)
            if existing is None or int(existing["fact_id"]) != result.row_id:
                raise ValueError("recovered fact identity changed during batch write")
            self._assert_existing_recovered_fact(existing, claim.fact)
            self._assert_existing_work_claim(
                connection,
                fact_id=result.row_id,
                value=claim,
            )
            link_id = self._assert_locked_recovery_link(
                connection,
                fact_id=result.row_id,
                session_id=value.session_id,
                message=(
                    "recovered fact identity exists without the identical "
                    "high-confidence link"
                ),
            )
            return (
                result,
                WriteResult(
                    "noop",
                    link_id,
                    self.canonical_sequence(connection),
                ),
            )

        connection.execute(
            "INSERT INTO work_claims(fact_id, event_kind, status, title, objective, summary, blocker, next_step, "
            "work_id, section_id, outcome_axis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (result.row_id, *self._work_claim_columns(claim)),
        )
        locked_link = self._locked_recovery_link(
            fact_id=result.row_id,
            session_id=value.session_id,
        )
        link_result = self._link_fact_to_session_in_transaction(
            connection,
            locked_link,
        )
        if link_result.disposition not in {"inserted", "noop"}:
            raise ValueError("recovered fact-session link was not accepted")
        self._assert_locked_recovery_link(
            connection,
            fact_id=result.row_id,
            session_id=value.session_id,
            message=(
                "recovered fact must finish with exactly one valid "
                "high-confidence link"
            ),
        )
        return result, link_result

    def append_recovered_work_claims(
        self,
        values: Sequence[RecoveredWorkClaimInput],
    ) -> tuple[tuple[WriteResult, WriteResult], ...]:
        """Atomically append a complete candidate-recovery batch.

        Every expected failure is discovered during a read-only preflight.
        The subsequent writes share one ``BEGIN IMMEDIATE`` transaction, so a
        rejected fact or link rolls back the entire batch. Replaying identical
        input produces only ``noop`` results and no SQLite row changes.
        """

        prepared = tuple(values)
        if not prepared:
            return ()
        with self.store.transaction(write=True) as connection:
            self._preflight_recovered_work_claims(connection, prepared)
            return tuple(
                self._append_recovered_work_claim_in_transaction(connection, value)
                for value in prepared
            )

    def append_recovered_work_claim(
        self,
        value: WorkClaimInput,
        link: FactSessionLinkInput,
    ) -> tuple[WriteResult, WriteResult]:
        """Atomically append one log-evidenced legacy claim and its only scope.

        This deliberately narrow migration interface prevents a crash from
        leaving a recovered fact without a session link.  It also refuses to
        let a correction inherit an ``exact`` (or otherwise different) scope
        from its predecessor.  Runtime writers do not call this method.
        """

        if value.fact.transport != RECOVERY_FACT_TRANSPORT:
            raise ValueError("recovered work claims require the recovery transport")
        if (
            link.method != RECOVERY_LINK_METHOD
            or link.confidence != "high"
            or link.rule_version != RECOVERY_LINK_RULE_VERSION
            or link.validation_state != "valid"
            or link.veto_reason is not None
        ):
            raise ValueError("recovered work claims require the locked high-confidence link")
        results = self.append_recovered_work_claims(
            (RecoveredWorkClaimInput(claim=value, session_id=link.session_id),)
        )
        return results[0]

    @staticmethod
    def usage_content_hash(value: UsageMeasurementInput) -> bytes:
        content = dataclass_dict(value)
        # Cursor/order/revision timestamps are ingestion provenance, not usage
        # truth. Re-observing the same cumulative counters at a newer poll must
        # be a physical no-op rather than manufacturing canonical history.
        for field_name in (
            "source_order",
            "source_revision_id",
            "observed_at_us",
            "updated_at_us",
        ):
            content.pop(field_name)
        return canonical_hash(content)

    @staticmethod
    def _usage_columns(value: UsageMeasurementInput, content_hash: bytes) -> tuple[object, ...]:
        return (
            value.session_id,
            value.lane,
            value.representation,
            value.update_semantics,
            value.precedence_role,
            value.granularity,
            int(value.totals_eligible),
            value.provider,
            value.model,
            value.usage_confidence,
            value.held_reason,
            value.input_tokens,
            int(value.input_tokens_reported),
            value.output_tokens,
            int(value.output_tokens_reported),
            value.cached_input_tokens,
            int(value.cached_input_tokens_reported),
            value.cache_creation_input_tokens,
            int(value.cache_creation_input_tokens_reported),
            value.cache_read_input_tokens,
            int(value.cache_read_input_tokens_reported),
            value.reasoning_output_tokens,
            int(value.reasoning_output_tokens_reported),
            value.total_tokens,
            int(value.total_tokens_reported),
            value.client_reported_cost_microusd,
            value.source_order,
            value.source_revision_id,
            content_hash,
            value.observed_at_us,
            value.updated_at_us,
        )

    @staticmethod
    def _usage_insert_sql() -> str:
        return (
            "INSERT INTO usage_measurements(session_id, lane, representation, update_semantics, precedence_role, "
            "granularity, totals_eligible, provider, model, usage_confidence, held_reason, input_tokens, "
            "input_tokens_reported, output_tokens, output_tokens_reported, cached_input_tokens, "
            "cached_input_tokens_reported, cache_creation_input_tokens, cache_creation_input_tokens_reported, "
            "cache_read_input_tokens, cache_read_input_tokens_reported, reasoning_output_tokens, "
            "reasoning_output_tokens_reported, total_tokens, total_tokens_reported, client_reported_cost_microusd, "
            "source_order, source_revision_id, content_hash, observed_at_us, updated_at_us) "
            "VALUES (" + ",".join("?" for _ in range(31)) + ")"
        )

    def _insert_checkpoint(
        self,
        connection: sqlite3.Connection,
        *,
        usage_measurement_id: int,
        boundary_kind: str,
        boundary_key: str,
        content_hash: bytes,
    ) -> bool:
        existing = connection.execute(
            "SELECT usage_checkpoint_id FROM usage_checkpoints WHERE usage_measurement_id = ? "
            "AND boundary_kind = ? AND boundary_key = ? AND content_hash = ?",
            (usage_measurement_id, boundary_kind, boundary_key, content_hash),
        ).fetchone()
        if existing is not None:
            return False
        measurement = connection.execute(
            "SELECT * FROM usage_measurements WHERE usage_measurement_id = ?",
            (usage_measurement_id,),
        ).fetchone()
        if measurement is None or bytes(measurement["content_hash"]) != content_hash:
            raise ValueError("checkpoint must snapshot the current usage content hash")
        connection.execute(
            "INSERT INTO usage_checkpoints(usage_measurement_id, boundary_kind, boundary_key, content_hash, "
            "lane, representation, precedence_role, provider, model, usage_confidence, totals_eligible, held_reason, input_tokens, input_tokens_reported, "
            "output_tokens, output_tokens_reported, cached_input_tokens, cached_input_tokens_reported, "
            "cache_creation_input_tokens, cache_creation_input_tokens_reported, cache_read_input_tokens, "
            "cache_read_input_tokens_reported, reasoning_output_tokens, reasoning_output_tokens_reported, "
            "total_tokens, total_tokens_reported, client_reported_cost_microusd, observed_at_us, updated_at_us, "
            "created_at_us) VALUES (" + ",".join("?" for _ in range(30)) + ")",
            (
                usage_measurement_id,
                boundary_kind,
                boundary_key,
                content_hash,
                measurement["lane"],
                measurement["representation"],
                measurement["precedence_role"],
                measurement["provider"],
                measurement["model"],
                measurement["usage_confidence"],
                measurement["totals_eligible"],
                measurement["held_reason"],
                measurement["input_tokens"],
                measurement["input_tokens_reported"],
                measurement["output_tokens"],
                measurement["output_tokens_reported"],
                measurement["cached_input_tokens"],
                measurement["cached_input_tokens_reported"],
                measurement["cache_creation_input_tokens"],
                measurement["cache_creation_input_tokens_reported"],
                measurement["cache_read_input_tokens"],
                measurement["cache_read_input_tokens_reported"],
                measurement["reasoning_output_tokens"],
                measurement["reasoning_output_tokens_reported"],
                measurement["total_tokens"],
                measurement["total_tokens_reported"],
                measurement["client_reported_cost_microusd"],
                measurement["observed_at_us"],
                measurement["updated_at_us"],
                now_us(),
            ),
        )
        return True

    def reconcile_usage(
        self,
        value: UsageMeasurementInput,
        *,
        checkpoint: tuple[str, str] | None = None,
    ) -> WriteResult:
        if value.update_semantics != "cumulative_snapshot":
            raise ValueError(
                "atomic_increment usage must use the immutable atomic-event lane; "
                "the cumulative reconcile interface refuses it"
            )
        content_hash = self.usage_content_hash(value)
        with self.store.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM usage_measurements WHERE session_id = ? AND lane = ? AND representation = ?",
                (value.session_id, value.lane, value.representation),
            ).fetchone()
            if value.totals_eligible:
                overlapping = connection.execute(
                    "SELECT usage_measurement_id, representation, content_hash, source_order "
                    "FROM usage_measurements "
                    "WHERE session_id = ? AND lane = ? AND totals_eligible = 1 "
                    "AND (? IS NULL OR usage_measurement_id <> ?)",
                    (
                        value.session_id,
                        value.lane,
                        int(existing["usage_measurement_id"]) if existing is not None else None,
                        int(existing["usage_measurement_id"]) if existing is not None else None,
                    ),
                ).fetchone()
                if overlapping is not None:
                    overlap_order = overlapping["source_order"]
                    ordered_supersession = value.source_order is not None and (
                        overlap_order is None
                        or int(value.source_order) > int(overlap_order)
                    )
                    if ordered_supersession:
                        # A representation TRANSITION, not a fork: the v1 row
                        # identity excludes representation, so a strictly
                        # newer observation under a new representation means
                        # the source replaced its row (e.g. codex sqlite
                        # fallback -> rollout). Demote the superseded row out
                        # of the totals lane instead of refusing the current
                        # truth on every rescan — refusal here re-minted a
                        # conflict per watcher tick, which is exactly the
                        # churn class this store exists to kill.
                        connection.execute(
                            "UPDATE usage_measurements SET totals_eligible = 0, "
                            "precedence_role = 'enrichment', "
                            "held_reason = 'superseded_totals_representation', "
                            "updated_at_us = MAX(observed_at_us, ?) "
                            "WHERE usage_measurement_id = ?",
                            (now_us(), int(overlapping["usage_measurement_id"])),
                        )
                        self._advance_sequence(connection)
                    else:
                        source = connection.execute(
                            "SELECT s.source_instance_id FROM sessions s WHERE s.session_id = ?", (value.session_id,)
                        ).fetchone()
                        if source is None:
                            raise ValueError("usage session does not exist")
                        conflict_id, sequence = self._record_conflict(
                            connection,
                            source_instance_id=int(source[0]),
                            native_entity_kind="usage_precedence",
                            native_entity_key=f"{value.session_id}:{value.lane}",
                            incumbent_hash=bytes(overlapping["content_hash"]),
                            incoming_hash=content_hash,
                            reason="multiple_totals_eligible_representations",
                        )
                        return WriteResult("conflict", int(overlapping["usage_measurement_id"]), sequence, conflict_id)
            if existing is None:
                cursor = connection.execute(self._usage_insert_sql(), self._usage_columns(value, content_hash))
                row_id = int(cursor.lastrowid)
                if checkpoint is not None:
                    self._insert_checkpoint(
                        connection,
                        usage_measurement_id=row_id,
                        boundary_kind=checkpoint[0],
                        boundary_key=checkpoint[1],
                        content_hash=content_hash,
                    )
                return WriteResult("inserted", row_id, self._advance_sequence(connection))
            row_id = int(existing["usage_measurement_id"])
            incumbent_hash = bytes(existing["content_hash"])
            if incumbent_hash == content_hash:
                incumbent_order = existing["source_order"]
                advances_high_watermark = value.source_order is not None and (
                    incumbent_order is None or int(value.source_order) > int(incumbent_order)
                )
                if advances_high_watermark:
                    # These fields are ingestion provenance, not usage truth.
                    # Persist the high-watermark without moving canonical
                    # sequence so a later stale divergent snapshot cannot
                    # overwrite the last-good counters after a restart.
                    connection.execute(
                        "UPDATE usage_measurements SET source_order = ?, "
                        "source_revision_id = COALESCE(?, source_revision_id) "
                        "WHERE usage_measurement_id = ?",
                        (value.source_order, value.source_revision_id, row_id),
                    )
                if checkpoint is not None and self._insert_checkpoint(
                    connection,
                    usage_measurement_id=row_id,
                    boundary_kind=checkpoint[0],
                    boundary_key=checkpoint[1],
                    content_hash=content_hash,
                ):
                    return WriteResult("updated", row_id, self._advance_sequence(connection))
                return WriteResult("noop", row_id, self.canonical_sequence(connection))
            incumbent_order = existing["source_order"]
            ordered_newer = value.source_order is not None and (
                incumbent_order is None or int(value.source_order) > int(incumbent_order)
            )
            if not ordered_newer:
                source = connection.execute(
                    "SELECT source_instance_id FROM sessions WHERE session_id = ?", (value.session_id,)
                ).fetchone()
                if source is None:
                    raise ValueError("usage session does not exist")
                conflict_id, sequence = self._record_conflict(
                    connection,
                    source_instance_id=int(source[0]),
                    native_entity_kind="usage_measurement",
                    native_entity_key=f"{value.session_id}:{value.lane}:{value.representation}",
                    incumbent_hash=incumbent_hash,
                    incoming_hash=content_hash,
                    reason="unordered_content_change",
                )
                return WriteResult("conflict", row_id, sequence, conflict_id)
            assignment_columns = (
                "update_semantics = ?, precedence_role = ?, granularity = ?, totals_eligible = ?, provider = ?, model = ?, "
                "usage_confidence = ?, held_reason = ?, input_tokens = ?, input_tokens_reported = ?, output_tokens = ?, "
                "output_tokens_reported = ?, cached_input_tokens = ?, cached_input_tokens_reported = ?, "
                "cache_creation_input_tokens = ?, cache_creation_input_tokens_reported = ?, cache_read_input_tokens = ?, "
                "cache_read_input_tokens_reported = ?, reasoning_output_tokens = ?, reasoning_output_tokens_reported = ?, "
                "total_tokens = ?, total_tokens_reported = ?, client_reported_cost_microusd = ?, source_order = ?, "
                "source_revision_id = ?, content_hash = ?, observed_at_us = ?, updated_at_us = ?"
            )
            values = self._usage_columns(value, content_hash)
            connection.execute(
                f"UPDATE usage_measurements SET {assignment_columns} WHERE usage_measurement_id = ?",
                (*values[3:], row_id),
            )
            if checkpoint is not None:
                self._insert_checkpoint(
                    connection,
                    usage_measurement_id=row_id,
                    boundary_kind=checkpoint[0],
                    boundary_key=checkpoint[1],
                    content_hash=content_hash,
                )
            return WriteResult("updated", row_id, self._advance_sequence(connection))

    def reconcile_usage_replay_batch(self, value: UsageMeasurementInput, *, attempts: int) -> dict[str, int]:
        """Collapse an equivalent replay storm before the SQLite write boundary."""

        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        before_changes = self.connection.total_changes
        before_sequence = self.canonical_sequence()
        first = self.reconcile_usage(value)
        expected_hash = self.usage_content_hash(value)
        row = self.connection.execute(
            "SELECT content_hash FROM usage_measurements WHERE session_id = ? AND lane = ? AND representation = ?",
            (value.session_id, value.lane, value.representation),
        ).fetchone()
        if row is None or bytes(row[0]) != expected_hash:
            raise RuntimeError("replay batch did not converge on the expected measurement")
        after_sequence = self.canonical_sequence()
        return {
            "attempts": attempts,
            "canonical_writes": self.connection.total_changes - before_changes,
            "canonical_sequence_delta": after_sequence - before_sequence,
            "inserted": int(first.disposition == "inserted"),
            "noops": attempts - int(first.disposition == "inserted"),
        }

    def record_cost(self, value: CostCalculationInput) -> WriteResult:
        with self.store.transaction(write=True) as connection:
            usage = connection.execute(
                "SELECT usage_measurement_id, totals_eligible, content_hash FROM usage_measurements "
                "WHERE usage_measurement_id = ?",
                (value.usage_measurement_id,),
            ).fetchone()
            if usage is None:
                raise ValueError("cost calculation references unknown usage")
            if not bool(usage["totals_eligible"]):
                return WriteResult("noop", None, self.canonical_sequence(connection))
            if bytes(usage["content_hash"]) != value.usage_content_hash:
                return WriteResult("conflict", value.usage_measurement_id, self.canonical_sequence(connection))
            existing = connection.execute(
                "SELECT cost_calculation_id, completeness, result_microusd FROM cost_calculations "
                "WHERE usage_content_hash = ? AND price_source = ? AND price_version = ? "
                "AND price_effective_at_us = ? AND formula_version = ? AND currency = ?",
                (
                    value.usage_content_hash,
                    value.price_source,
                    value.price_version,
                    value.price_effective_at_us,
                    value.formula_version,
                    value.currency,
                ),
            ).fetchone()
            if existing is not None:
                if existing["completeness"] == value.completeness and existing["result_microusd"] == value.result_microusd:
                    return WriteResult("noop", int(existing["cost_calculation_id"]), self.canonical_sequence(connection))
                return WriteResult("conflict", int(existing["cost_calculation_id"]), self.canonical_sequence(connection))
            cursor = connection.execute(
                "INSERT INTO cost_calculations(usage_measurement_id, usage_content_hash, price_source, price_version, "
                "price_effective_at_us, formula_version, currency, completeness, result_microusd, calculated_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    value.usage_measurement_id,
                    value.usage_content_hash,
                    value.price_source,
                    value.price_version,
                    value.price_effective_at_us,
                    value.formula_version,
                    value.currency,
                    value.completeness,
                    value.result_microusd,
                    value.calculated_at_us,
                ),
            )
            return WriteResult("inserted", int(cursor.lastrowid), self._advance_sequence(connection))

    def rebuild_minimal_read_models(self) -> dict[str, int]:
        """Rebuild only rm_task_current and rm_usage_day from canonical rows."""

        with self.store.transaction(write=True) as connection:
            sequence = self.canonical_sequence(connection)
            connection.execute("DELETE FROM rm_task_current")
            connection.execute("DELETE FROM rm_usage_day")
            connection.execute(
                """
                WITH RECURSIVE task_sessions(task_anchor_id, session_id) AS (
                    SELECT task_anchor_id, primary_session_id FROM task_anchors
                    UNION
                    SELECT ts.task_anchor_id, edge.child_session_id
                    FROM task_sessions ts
                    JOIN session_edges edge ON edge.parent_session_id = ts.session_id
                    WHERE edge.validation_state = 'valid'
                ), fact_lineage(fact_id, logical_slot_fact_id, logical_slot_occurred_at_us) AS (
                    SELECT fact_id, fact_id, occurred_at_us
                    FROM facts
                    WHERE supersedes_fact_id IS NULL
                    UNION ALL
                    SELECT
                        correction.fact_id,
                        lineage.logical_slot_fact_id,
                        lineage.logical_slot_occurred_at_us
                    FROM facts correction
                    JOIN fact_lineage lineage
                        ON correction.supersedes_fact_id = lineage.fact_id
                ), ranked_usage AS (
                    SELECT
                        usage.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY usage.session_id, usage.lane
                            ORDER BY
                                usage.totals_eligible DESC,
                                CASE usage.precedence_role
                                    WHEN 'authoritative' THEN 0
                                    WHEN 'fallback' THEN 1
                                    ELSE 2
                                END,
                                usage.updated_at_us DESC,
                                usage.usage_measurement_id DESC
                        ) AS representation_rank
                    FROM usage_measurements usage
                ), selected_usage AS (
                    -- Select one whole representation for every activity/lane.
                    -- An eligible fallback wins over an ineligible partial
                    -- authoritative row; otherwise authoritative precedence is
                    -- retained.  This keeps explicit zeroes and missing flags
                    -- visible without blending overlapping representations.
                    SELECT * FROM ranked_usage WHERE representation_rank = 1
                ), ranked_task_claims AS (
                    SELECT
                        task_sessions.task_anchor_id,
                        claim.status,
                        claim.title,
                        ROW_NUMBER() OVER (
                            PARTITION BY task_sessions.task_anchor_id
                            ORDER BY
                                lineage.logical_slot_occurred_at_us DESC,
                                lineage.logical_slot_fact_id DESC
                        ) AS claim_rank
                    FROM task_sessions
                    JOIN fact_session_links link
                        ON link.session_id = task_sessions.session_id
                        AND link.validation_state = 'valid'
                        AND link.confidence NOT IN ('ambiguous', 'rejected', 'conflict')
                    JOIN facts fact ON fact.fact_id = link.fact_id
                    JOIN fact_lineage lineage ON lineage.fact_id = fact.fact_id
                    JOIN work_claims claim ON claim.fact_id = fact.fact_id
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM facts successor
                        JOIN fact_session_links successor_link
                            ON successor_link.fact_id = successor.fact_id
                            AND successor_link.session_id = link.session_id
                            AND successor_link.validation_state = 'valid'
                            AND successor_link.confidence NOT IN ('ambiguous', 'rejected', 'conflict')
                        WHERE successor.supersedes_fact_id = fact.fact_id
                    )
                ), latest_task_claim AS (
                    SELECT task_anchor_id, status, title
                    FROM ranked_task_claims
                    WHERE claim_rank = 1
                ), task_findings AS (
                    SELECT task_sessions.task_anchor_id, COUNT(*) AS finding_count
                    FROM task_sessions
                    JOIN fact_session_links link
                        ON link.session_id = task_sessions.session_id
                        AND link.validation_state = 'valid'
                        AND link.confidence NOT IN ('ambiguous', 'rejected', 'conflict')
                    JOIN finding_actions finding ON finding.fact_id = link.fact_id
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM facts successor
                        JOIN fact_session_links successor_link
                            ON successor_link.fact_id = successor.fact_id
                            AND successor_link.session_id = link.session_id
                            AND successor_link.validation_state = 'valid'
                            AND successor_link.confidence NOT IN ('ambiguous', 'rejected', 'conflict')
                        WHERE successor.supersedes_fact_id = finding.fact_id
                    )
                    GROUP BY task_sessions.task_anchor_id
                ), task_rollup AS (
                    SELECT
                        anchor.task_anchor_id,
                        anchor.public_task_id,
                        anchor.primary_session_id,
                        MAX(session.last_activity_at_us) AS last_activity_at_us,
                        MAX(session.sort_activity_at_us) AS sort_activity_at_us,
                        COUNT(DISTINCT task_sessions.session_id) AS session_count,
                        COUNT(DISTINCT usage.usage_measurement_id) AS usage_measurement_count,
                        CASE
                            WHEN SUM(CASE WHEN usage.totals_eligible = 1 AND usage.input_tokens_reported = 1 THEN 1 ELSE 0 END) > 0
                                THEN SUM(CASE WHEN usage.totals_eligible = 1 AND usage.input_tokens_reported = 1 THEN usage.input_tokens ELSE 0 END)
                            WHEN SUM(CASE WHEN usage.totals_eligible = 0 AND usage.input_tokens_reported = 1 AND usage.input_tokens = 0 THEN 1 ELSE 0 END) > 0
                                AND SUM(CASE WHEN usage.totals_eligible = 0 AND usage.input_tokens_reported = 1 AND usage.input_tokens <> 0 THEN 1 ELSE 0 END) = 0
                                THEN 0
                            ELSE NULL
                        END AS input_tokens,
                        CASE
                            WHEN SUM(CASE WHEN usage.totals_eligible = 1 AND usage.output_tokens_reported = 1 THEN 1 ELSE 0 END) > 0
                                THEN SUM(CASE WHEN usage.totals_eligible = 1 AND usage.output_tokens_reported = 1 THEN usage.output_tokens ELSE 0 END)
                            WHEN SUM(CASE WHEN usage.totals_eligible = 0 AND usage.output_tokens_reported = 1 AND usage.output_tokens = 0 THEN 1 ELSE 0 END) > 0
                                AND SUM(CASE WHEN usage.totals_eligible = 0 AND usage.output_tokens_reported = 1 AND usage.output_tokens <> 0 THEN 1 ELSE 0 END) = 0
                                THEN 0
                            ELSE NULL
                        END AS output_tokens,
                        CASE
                            WHEN SUM(CASE WHEN usage.totals_eligible = 1 AND usage.total_tokens_reported = 1 THEN 1 ELSE 0 END) > 0
                                THEN SUM(CASE WHEN usage.totals_eligible = 1 AND usage.total_tokens_reported = 1 THEN usage.total_tokens ELSE 0 END)
                            WHEN SUM(CASE WHEN usage.totals_eligible = 0 AND usage.total_tokens_reported = 1 AND usage.total_tokens = 0 THEN 1 ELSE 0 END) > 0
                                AND SUM(CASE WHEN usage.totals_eligible = 0 AND usage.total_tokens_reported = 1 AND usage.total_tokens <> 0 THEN 1 ELSE 0 END) = 0
                                THEN 0
                            ELSE NULL
                        END AS total_tokens,
                        COALESCE(SUM(
                            (1 - usage.input_tokens_reported)
                            + (1 - usage.output_tokens_reported)
                            + (1 - usage.cached_input_tokens_reported)
                            + (1 - usage.cache_creation_input_tokens_reported)
                            + (1 - usage.cache_read_input_tokens_reported)
                            + (1 - usage.reasoning_output_tokens_reported)
                            + (1 - usage.total_tokens_reported)
                        ), 0) AS usage_missing_field_count
                    FROM task_anchors anchor
                    JOIN task_sessions ON task_sessions.task_anchor_id = anchor.task_anchor_id
                    JOIN sessions session ON session.session_id = task_sessions.session_id
                    LEFT JOIN selected_usage usage
                        ON usage.session_id = task_sessions.session_id
                    GROUP BY anchor.task_anchor_id, anchor.public_task_id, anchor.primary_session_id
                )
                INSERT INTO rm_task_current(
                    public_task_id, task_anchor_id, primary_session_id, title, projected_state,
                    last_activity_at_us, sort_activity_at_us, session_count, usage_measurement_count,
                    input_tokens, output_tokens, total_tokens, usage_missing_field_count,
                    finding_count, built_through_sequence
                )
                SELECT
                    task_rollup.public_task_id,
                    task_rollup.task_anchor_id,
                    task_rollup.primary_session_id,
                    latest_task_claim.title,
                    CASE latest_task_claim.status
                        WHEN 'completed' THEN 'reported_complete'
                        WHEN 'blocked' THEN 'partial_needs_review'
                        WHEN 'aborted' THEN 'aborted'
                        ELSE 'active'
                    END,
                    task_rollup.last_activity_at_us,
                    COALESCE(task_rollup.sort_activity_at_us, 0),
                    task_rollup.session_count,
                    task_rollup.usage_measurement_count,
                    task_rollup.input_tokens,
                    task_rollup.output_tokens,
                    task_rollup.total_tokens,
                    task_rollup.usage_missing_field_count,
                    COALESCE(task_findings.finding_count, 0),
                    ?
                FROM task_rollup
                LEFT JOIN latest_task_claim
                    ON latest_task_claim.task_anchor_id = task_rollup.task_anchor_id
                LEFT JOIN task_findings
                    ON task_findings.task_anchor_id = task_rollup.task_anchor_id
                """,
                (sequence,),
            )
            connection.execute(
                """
                WITH ranked_day_checkpoints AS (
                    SELECT
                        checkpoint.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY checkpoint.usage_measurement_id, checkpoint.boundary_key
                            ORDER BY checkpoint.created_at_us DESC, checkpoint.usage_checkpoint_id DESC
                        ) AS checkpoint_rank
                    FROM usage_checkpoints checkpoint
                    WHERE checkpoint.boundary_kind = 'day'
                ), timeline_candidates AS (
                    SELECT
                        checkpoint.usage_measurement_id,
                        measurement.session_id,
                        checkpoint.precedence_role,
                        checkpoint.provider,
                        checkpoint.model,
                        checkpoint.totals_eligible,
                        checkpoint.input_tokens,
                        checkpoint.input_tokens_reported,
                        checkpoint.output_tokens,
                        checkpoint.output_tokens_reported,
                        checkpoint.cached_input_tokens,
                        checkpoint.cached_input_tokens_reported,
                        checkpoint.cache_creation_input_tokens,
                        checkpoint.cache_creation_input_tokens_reported,
                        checkpoint.cache_read_input_tokens,
                        checkpoint.cache_read_input_tokens_reported,
                        checkpoint.reasoning_output_tokens,
                        checkpoint.reasoning_output_tokens_reported,
                        checkpoint.total_tokens,
                        checkpoint.total_tokens_reported,
                        checkpoint.content_hash,
                        checkpoint.boundary_key AS day,
                        checkpoint.created_at_us AS timeline_order_us,
                        checkpoint.usage_checkpoint_id AS timeline_tie_id,
                        0 AS is_current
                    FROM ranked_day_checkpoints checkpoint
                    JOIN usage_measurements measurement
                        ON measurement.usage_measurement_id = checkpoint.usage_measurement_id
                    WHERE checkpoint.checkpoint_rank = 1
                    UNION ALL
                    SELECT
                        usage.usage_measurement_id,
                        usage.session_id,
                        usage.precedence_role,
                        usage.provider,
                        usage.model,
                        usage.totals_eligible,
                        usage.input_tokens,
                        usage.input_tokens_reported,
                        usage.output_tokens,
                        usage.output_tokens_reported,
                        usage.cached_input_tokens,
                        usage.cached_input_tokens_reported,
                        usage.cache_creation_input_tokens,
                        usage.cache_creation_input_tokens_reported,
                        usage.cache_read_input_tokens,
                        usage.cache_read_input_tokens_reported,
                        usage.reasoning_output_tokens,
                        usage.reasoning_output_tokens_reported,
                        usage.total_tokens,
                        usage.total_tokens_reported,
                        usage.content_hash,
                        strftime('%Y-%m-%d', usage.updated_at_us / 1000000, 'unixepoch') AS day,
                        usage.updated_at_us AS timeline_order_us,
                        usage.usage_measurement_id AS timeline_tie_id,
                        1 AS is_current
                    FROM usage_measurements usage
                ), ranked_timeline AS (
                    SELECT
                        timeline.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY timeline.usage_measurement_id, timeline.day
                            ORDER BY timeline.is_current DESC, timeline.timeline_order_us DESC,
                                     timeline.timeline_tie_id DESC
                        ) AS timeline_rank
                    FROM timeline_candidates timeline
                ), cumulative_day AS (
                    SELECT * FROM ranked_timeline WHERE timeline_rank = 1
                ), ranked_cost AS (
                    SELECT
                        calculation.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY calculation.usage_measurement_id, calculation.usage_content_hash
                            ORDER BY calculation.calculated_at_us DESC, calculation.cost_calculation_id DESC
                        ) AS calculation_rank
                    FROM cost_calculations calculation
                ), latest_cost AS (
                    SELECT * FROM ranked_cost WHERE calculation_rank = 1
                ), cumulative_with_cost AS (
                    SELECT
                        usage.*,
                        latest_cost.result_microusd AS cumulative_cost_microusd,
                        latest_cost.completeness AS cumulative_cost_completeness,
                        latest_cost.price_source,
                        latest_cost.price_version,
                        latest_cost.price_effective_at_us,
                        latest_cost.formula_version,
                        latest_cost.currency
                    FROM cumulative_day usage
                    LEFT JOIN latest_cost
                        ON latest_cost.usage_measurement_id = usage.usage_measurement_id
                        AND latest_cost.usage_content_hash = usage.content_hash
                ), with_previous AS (
                    SELECT
                        usage.*,
                        LAG(day) OVER measurement_days AS previous_day,
                        LAG(input_tokens) OVER measurement_days AS previous_input_tokens,
                        LAG(input_tokens_reported) OVER measurement_days AS previous_input_reported,
                        LAG(output_tokens) OVER measurement_days AS previous_output_tokens,
                        LAG(output_tokens_reported) OVER measurement_days AS previous_output_reported,
                        LAG(cached_input_tokens) OVER measurement_days AS previous_cached_input_tokens,
                        LAG(cached_input_tokens_reported) OVER measurement_days AS previous_cached_input_reported,
                        LAG(cache_creation_input_tokens) OVER measurement_days AS previous_cache_creation_input_tokens,
                        LAG(cache_creation_input_tokens_reported) OVER measurement_days AS previous_cache_creation_input_reported,
                        LAG(cache_read_input_tokens) OVER measurement_days AS previous_cache_read_input_tokens,
                        LAG(cache_read_input_tokens_reported) OVER measurement_days AS previous_cache_read_input_reported,
                        LAG(reasoning_output_tokens) OVER measurement_days AS previous_reasoning_output_tokens,
                        LAG(reasoning_output_tokens_reported) OVER measurement_days AS previous_reasoning_output_reported,
                        LAG(total_tokens) OVER measurement_days AS previous_total_tokens,
                        LAG(total_tokens_reported) OVER measurement_days AS previous_total_reported,
                        LAG(cumulative_cost_microusd) OVER measurement_days AS previous_cost_microusd,
                        LAG(cumulative_cost_completeness) OVER measurement_days AS previous_cost_completeness,
                        LAG(price_source) OVER measurement_days AS previous_price_source,
                        LAG(price_version) OVER measurement_days AS previous_price_version,
                        LAG(price_effective_at_us) OVER measurement_days AS previous_price_effective_at_us,
                        LAG(formula_version) OVER measurement_days AS previous_formula_version,
                        LAG(currency) OVER measurement_days AS previous_currency
                    FROM cumulative_with_cost usage
                    WINDOW measurement_days AS (
                        PARTITION BY usage_measurement_id ORDER BY day
                    )
                ), daily_delta AS (
                    SELECT
                        usage.*,
                        CASE WHEN input_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_input_reported = 1 AND input_tokens >= previous_input_tokens)
                        ) THEN input_tokens - COALESCE(previous_input_tokens, 0) ELSE NULL END AS input_delta,
                        CASE WHEN input_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_input_reported = 1 AND input_tokens >= previous_input_tokens)
                        ) THEN 1 ELSE 0 END AS input_delta_reported,
                        CASE WHEN output_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_output_reported = 1 AND output_tokens >= previous_output_tokens)
                        ) THEN output_tokens - COALESCE(previous_output_tokens, 0) ELSE NULL END AS output_delta,
                        CASE WHEN output_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_output_reported = 1 AND output_tokens >= previous_output_tokens)
                        ) THEN 1 ELSE 0 END AS output_delta_reported,
                        CASE WHEN cached_input_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_cached_input_reported = 1 AND cached_input_tokens >= previous_cached_input_tokens)
                        ) THEN cached_input_tokens - COALESCE(previous_cached_input_tokens, 0) ELSE NULL END AS cached_input_delta,
                        CASE WHEN cached_input_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_cached_input_reported = 1 AND cached_input_tokens >= previous_cached_input_tokens)
                        ) THEN 1 ELSE 0 END AS cached_input_delta_reported,
                        CASE WHEN cache_creation_input_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_cache_creation_input_reported = 1 AND cache_creation_input_tokens >= previous_cache_creation_input_tokens)
                        ) THEN cache_creation_input_tokens - COALESCE(previous_cache_creation_input_tokens, 0) ELSE NULL END AS cache_creation_delta,
                        CASE WHEN cache_creation_input_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_cache_creation_input_reported = 1 AND cache_creation_input_tokens >= previous_cache_creation_input_tokens)
                        ) THEN 1 ELSE 0 END AS cache_creation_delta_reported,
                        CASE WHEN cache_read_input_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_cache_read_input_reported = 1 AND cache_read_input_tokens >= previous_cache_read_input_tokens)
                        ) THEN cache_read_input_tokens - COALESCE(previous_cache_read_input_tokens, 0) ELSE NULL END AS cache_read_delta,
                        CASE WHEN cache_read_input_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_cache_read_input_reported = 1 AND cache_read_input_tokens >= previous_cache_read_input_tokens)
                        ) THEN 1 ELSE 0 END AS cache_read_delta_reported,
                        CASE WHEN reasoning_output_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_reasoning_output_reported = 1 AND reasoning_output_tokens >= previous_reasoning_output_tokens)
                        ) THEN reasoning_output_tokens - COALESCE(previous_reasoning_output_tokens, 0) ELSE NULL END AS reasoning_output_delta,
                        CASE WHEN reasoning_output_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_reasoning_output_reported = 1 AND reasoning_output_tokens >= previous_reasoning_output_tokens)
                        ) THEN 1 ELSE 0 END AS reasoning_output_delta_reported,
                        CASE WHEN total_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_total_reported = 1 AND total_tokens >= previous_total_tokens)
                        ) THEN total_tokens - COALESCE(previous_total_tokens, 0) ELSE NULL END AS total_delta,
                        CASE WHEN total_tokens_reported = 1 AND (
                            previous_day IS NULL OR (previous_total_reported = 1 AND total_tokens >= previous_total_tokens)
                        ) THEN 1 ELSE 0 END AS total_delta_reported,
                        CASE WHEN cumulative_cost_microusd IS NOT NULL AND (
                            previous_day IS NULL OR (
                                previous_cost_microusd IS NOT NULL
                                AND cumulative_cost_microusd >= previous_cost_microusd
                                AND price_source = previous_price_source
                                AND price_version = previous_price_version
                                AND price_effective_at_us = previous_price_effective_at_us
                                AND formula_version = previous_formula_version
                                AND currency = previous_currency
                            )
                        ) THEN cumulative_cost_microusd - COALESCE(previous_cost_microusd, 0) ELSE NULL END AS cost_delta,
                        CASE WHEN cumulative_cost_microusd IS NOT NULL AND (
                            previous_day IS NULL OR (
                                previous_cost_microusd IS NOT NULL
                                AND cumulative_cost_microusd >= previous_cost_microusd
                                AND price_source = previous_price_source
                                AND price_version = previous_price_version
                                AND price_effective_at_us = previous_price_effective_at_us
                                AND formula_version = previous_formula_version
                                AND currency = previous_currency
                            )
                        ) THEN 1 ELSE 0 END AS cost_delta_reported
                    FROM with_previous usage
                ), projection_rows AS (
                    SELECT
                        usage.*,
                        usage.precedence_role || CASE
                            WHEN usage.totals_eligible = 1 THEN '_cumulative_delta'
                            ELSE '_held_non_additive'
                        END AS projected_usage_basis,
                        CASE
                            WHEN usage.totals_eligible = 1 THEN usage.input_delta
                            WHEN usage.input_tokens_reported = 1 AND usage.input_tokens = 0 THEN 0
                            ELSE NULL
                        END AS projected_input_tokens,
                        CASE WHEN usage.totals_eligible = 1
                            THEN usage.input_delta_reported ELSE usage.input_tokens_reported END AS projected_input_reported,
                        CASE
                            WHEN usage.totals_eligible = 1 THEN usage.output_delta
                            WHEN usage.output_tokens_reported = 1 AND usage.output_tokens = 0 THEN 0
                            ELSE NULL
                        END AS projected_output_tokens,
                        CASE WHEN usage.totals_eligible = 1
                            THEN usage.output_delta_reported ELSE usage.output_tokens_reported END AS projected_output_reported,
                        CASE
                            WHEN usage.totals_eligible = 1 THEN usage.cached_input_delta
                            WHEN usage.cached_input_tokens_reported = 1 AND usage.cached_input_tokens = 0 THEN 0
                            ELSE NULL
                        END AS projected_cached_input_tokens,
                        CASE WHEN usage.totals_eligible = 1
                            THEN usage.cached_input_delta_reported ELSE usage.cached_input_tokens_reported END AS projected_cached_input_reported,
                        CASE
                            WHEN usage.totals_eligible = 1 THEN usage.cache_creation_delta
                            WHEN usage.cache_creation_input_tokens_reported = 1 AND usage.cache_creation_input_tokens = 0 THEN 0
                            ELSE NULL
                        END AS projected_cache_creation_tokens,
                        CASE WHEN usage.totals_eligible = 1
                            THEN usage.cache_creation_delta_reported ELSE usage.cache_creation_input_tokens_reported END AS projected_cache_creation_reported,
                        CASE
                            WHEN usage.totals_eligible = 1 THEN usage.cache_read_delta
                            WHEN usage.cache_read_input_tokens_reported = 1 AND usage.cache_read_input_tokens = 0 THEN 0
                            ELSE NULL
                        END AS projected_cache_read_tokens,
                        CASE WHEN usage.totals_eligible = 1
                            THEN usage.cache_read_delta_reported ELSE usage.cache_read_input_tokens_reported END AS projected_cache_read_reported,
                        CASE
                            WHEN usage.totals_eligible = 1 THEN usage.reasoning_output_delta
                            WHEN usage.reasoning_output_tokens_reported = 1 AND usage.reasoning_output_tokens = 0 THEN 0
                            ELSE NULL
                        END AS projected_reasoning_output_tokens,
                        CASE WHEN usage.totals_eligible = 1
                            THEN usage.reasoning_output_delta_reported ELSE usage.reasoning_output_tokens_reported END AS projected_reasoning_output_reported,
                        CASE
                            WHEN usage.totals_eligible = 1 THEN usage.total_delta
                            WHEN usage.total_tokens_reported = 1 AND usage.total_tokens = 0 THEN 0
                            ELSE NULL
                        END AS projected_total_tokens,
                        CASE WHEN usage.totals_eligible = 1
                            THEN usage.total_delta_reported ELSE usage.total_tokens_reported END AS projected_total_reported,
                        CASE WHEN usage.totals_eligible = 1 THEN usage.cost_delta ELSE NULL END AS projected_cost_microusd,
                        CASE WHEN usage.totals_eligible = 1 THEN usage.cost_delta_reported ELSE 0 END AS projected_cost_reported
                    FROM daily_delta usage
                )
                INSERT INTO rm_usage_day(
                    day, client, provider, model, usage_basis, measurement_count,
                    held_measurement_count, input_tokens, input_reported_count, input_missing_count,
                    output_tokens, output_reported_count, output_missing_count,
                    cached_input_tokens, cache_reported_count, cache_missing_count,
                    cache_creation_input_tokens, cache_creation_reported_count, cache_creation_missing_count,
                    cache_read_input_tokens, cache_read_reported_count, cache_read_missing_count,
                    reasoning_output_tokens, reasoning_reported_count, reasoning_missing_count,
                    total_tokens, total_reported_count, total_missing_count,
                    estimated_cost_microusd, cost_completeness, built_through_sequence
                )
                SELECT
                    usage.day,
                    source.client,
                    usage.provider,
                    usage.model,
                    usage.projected_usage_basis,
                    COUNT(*),
                    SUM(CASE WHEN usage.totals_eligible = 0 THEN 1 ELSE 0 END),
                    CASE WHEN SUM(usage.projected_input_reported) > 0
                            AND SUM(CASE WHEN usage.projected_input_reported = 1 AND usage.projected_input_tokens IS NULL THEN 1 ELSE 0 END) = 0
                        THEN SUM(CASE WHEN usage.projected_input_reported = 1 THEN usage.projected_input_tokens ELSE 0 END) ELSE NULL END,
                    SUM(usage.projected_input_reported),
                    SUM(1 - usage.projected_input_reported),
                    CASE WHEN SUM(usage.projected_output_reported) > 0
                            AND SUM(CASE WHEN usage.projected_output_reported = 1 AND usage.projected_output_tokens IS NULL THEN 1 ELSE 0 END) = 0
                        THEN SUM(CASE WHEN usage.projected_output_reported = 1 THEN usage.projected_output_tokens ELSE 0 END) ELSE NULL END,
                    SUM(usage.projected_output_reported),
                    SUM(1 - usage.projected_output_reported),
                    CASE WHEN SUM(usage.projected_cached_input_reported) > 0
                            AND SUM(CASE WHEN usage.projected_cached_input_reported = 1 AND usage.projected_cached_input_tokens IS NULL THEN 1 ELSE 0 END) = 0
                        THEN SUM(CASE WHEN usage.projected_cached_input_reported = 1 THEN usage.projected_cached_input_tokens ELSE 0 END) ELSE NULL END,
                    SUM(usage.projected_cached_input_reported),
                    SUM(1 - usage.projected_cached_input_reported),
                    CASE WHEN SUM(usage.projected_cache_creation_reported) > 0
                            AND SUM(CASE WHEN usage.projected_cache_creation_reported = 1 AND usage.projected_cache_creation_tokens IS NULL THEN 1 ELSE 0 END) = 0
                        THEN SUM(CASE WHEN usage.projected_cache_creation_reported = 1 THEN usage.projected_cache_creation_tokens ELSE 0 END) ELSE NULL END,
                    SUM(usage.projected_cache_creation_reported),
                    SUM(1 - usage.projected_cache_creation_reported),
                    CASE WHEN SUM(usage.projected_cache_read_reported) > 0
                            AND SUM(CASE WHEN usage.projected_cache_read_reported = 1 AND usage.projected_cache_read_tokens IS NULL THEN 1 ELSE 0 END) = 0
                        THEN SUM(CASE WHEN usage.projected_cache_read_reported = 1 THEN usage.projected_cache_read_tokens ELSE 0 END) ELSE NULL END,
                    SUM(usage.projected_cache_read_reported),
                    SUM(1 - usage.projected_cache_read_reported),
                    CASE WHEN SUM(usage.projected_reasoning_output_reported) > 0
                            AND SUM(CASE WHEN usage.projected_reasoning_output_reported = 1 AND usage.projected_reasoning_output_tokens IS NULL THEN 1 ELSE 0 END) = 0
                        THEN SUM(CASE WHEN usage.projected_reasoning_output_reported = 1 THEN usage.projected_reasoning_output_tokens ELSE 0 END) ELSE NULL END,
                    SUM(usage.projected_reasoning_output_reported),
                    SUM(1 - usage.projected_reasoning_output_reported),
                    CASE WHEN SUM(usage.projected_total_reported) > 0
                            AND SUM(CASE WHEN usage.projected_total_reported = 1 AND usage.projected_total_tokens IS NULL THEN 1 ELSE 0 END) = 0
                        THEN SUM(CASE WHEN usage.projected_total_reported = 1 THEN usage.projected_total_tokens ELSE 0 END) ELSE NULL END,
                    SUM(usage.projected_total_reported),
                    SUM(1 - usage.projected_total_reported),
                    CASE WHEN SUM(usage.projected_cost_reported) > 0
                        THEN SUM(CASE WHEN usage.projected_cost_reported = 1 THEN usage.projected_cost_microusd ELSE 0 END) ELSE NULL END,
                    CASE
                        WHEN SUM(CASE WHEN usage.totals_eligible = 1 THEN 1 ELSE 0 END) = 0 THEN 'unavailable'
                        WHEN SUM(usage.projected_cost_reported) = 0 THEN 'unavailable'
                        WHEN SUM(usage.projected_cost_reported) = SUM(CASE WHEN usage.totals_eligible = 1 THEN 1 ELSE 0 END)
                            AND SUM(CASE WHEN usage.totals_eligible = 1 AND usage.cumulative_cost_completeness = 'complete' THEN 1 ELSE 0 END) = SUM(CASE WHEN usage.totals_eligible = 1 THEN 1 ELSE 0 END)
                            THEN 'complete'
                        ELSE 'partial'
                    END,
                    ?
                FROM projection_rows usage
                JOIN sessions session ON session.session_id = usage.session_id
                JOIN source_instances source ON source.source_instance_id = session.source_instance_id
                GROUP BY day, source.client, usage.provider, usage.model,
                         usage.projected_usage_basis
                """,
                (sequence,),
            )
            fingerprint = canonical_hash({"canonical_sequence": sequence, "projection_version": 1})
            for projection_name in MINIMAL_READ_MODEL_NAMES:
                connection.execute(
                    "INSERT INTO projection_generations(projection_name, projection_version, built_through_sequence, "
                    "input_fingerprint, state, built_at_us) VALUES (?, 1, ?, ?, 'current', ?) "
                    "ON CONFLICT(projection_name) DO UPDATE SET projection_version = excluded.projection_version, "
                    "built_through_sequence = excluded.built_through_sequence, input_fingerprint = excluded.input_fingerprint, "
                    "state = excluded.state, built_at_us = excluded.built_at_us",
                    (projection_name, sequence, fingerprint, now_us()),
                )
            task_count = int(connection.execute("SELECT COUNT(*) FROM rm_task_current").fetchone()[0])
            usage_day_count = int(connection.execute("SELECT COUNT(*) FROM rm_usage_day").fetchone()[0])
            return {"task_count": task_count, "usage_day_count": usage_day_count, "built_through_sequence": sequence}

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskCurrent:
        return TaskCurrent(
            public_task_id=str(row["public_task_id"]),
            task_anchor_id=int(row["task_anchor_id"]),
            primary_session_id=int(row["primary_session_id"]),
            title=row["title"],
            projected_state=str(row["projected_state"]),
            last_activity_at_us=row["last_activity_at_us"],
            sort_activity_at_us=int(row["sort_activity_at_us"]),
            session_count=int(row["session_count"]),
            usage_measurement_count=int(row["usage_measurement_count"]),
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
            usage_missing_field_count=int(row["usage_missing_field_count"]),
            finding_count=int(row["finding_count"]),
            built_through_sequence=int(row["built_through_sequence"]),
        )

    def get_task(self, public_task_id: str) -> TaskCurrent | None:
        row = self.connection.execute(
            _GET_TASK_SQL,
            (public_task_id,),
        ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def list_tasks(self, *, limit: int, before: TaskCursor | None = None) -> Sequence[TaskCurrent]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = before or TaskCursor(_MAX_SQLITE_INT, "\U0010ffff")
        rows = self.connection.execute(
            _LIST_TASKS_SQL,
            (cursor.sort_activity_at_us, cursor.public_task_id, limit),
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    @staticmethod
    def _usage_days_statement(
        query: UsageDayQuery,
    ) -> tuple[str, list[object]]:
        clauses = ["day >= ?", "day <= ?"]
        params: list[object] = [query.start_day, query.end_day]
        index_name = "idx_rm_usage_day_range"
        if query.client is not None:
            clauses.append("client = ?")
            params.append(query.client)
            index_name = "idx_rm_usage_client_range"
        if query.model is not None:
            clauses.append("model = ?")
            params.append(query.model)
            if query.client is None:
                index_name = "idx_rm_usage_model_range"
        params.append(query.limit)
        sql = (
            f"SELECT * FROM rm_usage_day INDEXED BY {index_name} "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY day DESC, rm_usage_day_id DESC LIMIT ?"
        )
        return sql, params

    def usage_days(self, query: UsageDayQuery) -> Sequence[Mapping[str, object]]:
        if not 1 <= query.limit <= 2_000:
            raise ValueError("usage day limit must be between 1 and 2000")
        sql, params = self._usage_days_statement(query)
        rows = self.connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_sessions(
        self,
        *,
        limit: int,
        before: tuple[int, int] | None = None,
    ) -> Sequence[Session]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2_000:
            raise ValueError("session limit must be between 1 and 2000")
        cursor = before or (_MAX_SQLITE_INT, _MAX_SQLITE_INT)
        rows = self.connection.execute(
            _LIST_SESSIONS_SQL,
            (cursor[0], cursor[1], limit),
        ).fetchall()
        return [self._session_from_row(row) for row in rows]

    @staticmethod
    def _by_ids_batch(ids: Sequence[int], what: str) -> list[int]:
        if any(isinstance(value, bool) or not isinstance(value, int) for value in ids):
            raise ValueError(f"{what} ids must be integers")
        if len(ids) > _BY_IDS_BATCH_LIMIT:
            raise ValueError(
                f"{what} batch must not exceed {_BY_IDS_BATCH_LIMIT} ids"
            )
        return [int(value) for value in ids]

    def get_sessions_by_ids(
        self, session_ids: Sequence[int]
    ) -> Mapping[int, Session]:
        """Primary-key point lookups for one fetched page's session ids."""

        batch = self._by_ids_batch(session_ids, "session")
        if not batch:
            return {}
        rows = self.connection.execute(
            _SESSIONS_BY_IDS_SQL.format(placeholders=",".join("?" * len(batch))),
            batch,
        ).fetchall()
        sessions = (self._session_from_row(row) for row in rows)
        return {session.session_id: session for session in sessions}

    def get_source_instances_by_ids(
        self, source_instance_ids: Sequence[int]
    ) -> Mapping[int, SourceInstance]:
        """Primary-key point lookups labeling sessions with client identity."""

        batch = self._by_ids_batch(source_instance_ids, "source instance")
        if not batch:
            return {}
        rows = self.connection.execute(
            _SOURCES_BY_IDS_SQL.format(placeholders=",".join("?" * len(batch))),
            batch,
        ).fetchall()
        sources = (self._source_from_row(row) for row in rows)
        return {source.source_instance_id: source for source in sources}

    def valid_lineage_edges(
        self, child_session_ids: Sequence[int]
    ) -> Mapping[int, tuple[int, str]]:
        """child session id -> (parent session id, relation) over VALIDATED
        edges — at most one per child by ``uq_session_edges_one_valid_parent``
        (relation-agnostic: continuation/retry children carry their lineage
        under those relations). Raw observed-lineage claims
        (``session_observed_lineage``) are pre-validation state and are
        deliberately not served here."""

        batch = self._by_ids_batch(child_session_ids, "session")
        if not batch:
            return {}
        rows = self.connection.execute(
            _VALID_LINEAGE_EDGES_SQL.format(placeholders=",".join("?" * len(batch))),
            batch,
        ).fetchall()
        return {
            int(row["child_session_id"]): (int(row["parent_session_id"]), str(row["relation"]))
            for row in rows
        }

    def health_summary(self) -> Mapping[str, object]:
        metadata = self.connection.execute(_HEALTH_STORE_SQL).fetchone()
        projection_rows = self.connection.execute(_HEALTH_PROJECTIONS_SQL).fetchall()
        sources = self.connection.execute(_HEALTH_SOURCES_SQL).fetchall()
        return {
            "store": dict(metadata) if metadata is not None else None,
            "projections": [dict(row) for row in projection_rows],
            "sources": [dict(row) for row in sources],
        }

    def explain_core_queries(self) -> Mapping[str, list[str]]:
        usage_queries = [
            self._usage_days_statement(
                UsageDayQuery("2026-01-01", "2026-12-31", limit=400)
            ),
            self._usage_days_statement(
                UsageDayQuery(
                    "2026-01-01",
                    "2026-12-31",
                    client="codex",
                    limit=400,
                )
            ),
            self._usage_days_statement(
                UsageDayQuery(
                    "2026-01-01",
                    "2026-12-31",
                    model="synthetic-model",
                    limit=400,
                )
            ),
            self._usage_days_statement(
                UsageDayQuery(
                    "2026-01-01",
                    "2026-12-31",
                    client="codex",
                    model="synthetic-model",
                    limit=400,
                )
            ),
        ]
        queries: dict[str, list[tuple[str, Sequence[object]]]] = {
            "work": [
                (
                    _LIST_TASKS_SQL,
                    (_MAX_SQLITE_INT, "\U0010ffff", 50),
                )
            ],
            "task": [(_GET_TASK_SQL, ("task_" + "0" * 32,))],
            "usage": usage_queries,
            "sessions": [
                (
                    _LIST_SESSIONS_SQL,
                    (_MAX_SQLITE_INT, _MAX_SQLITE_INT, 100),
                ),
                (_SESSIONS_BY_IDS_SQL.format(placeholders="?"), (1,)),
                (_SOURCES_BY_IDS_SQL.format(placeholders="?"), (1,)),
                (_VALID_LINEAGE_EDGES_SQL.format(placeholders="?"), (1,)),
            ],
            "health": [
                (_HEALTH_STORE_SQL, ()),
                (_HEALTH_PROJECTIONS_SQL, ()),
                (_HEALTH_SOURCES_SQL, ()),
            ],
        }
        result: dict[str, list[str]] = {}
        for name, statements in queries.items():
            details: list[str] = []
            for sql, params in statements:
                rows = self.connection.execute(
                    f"EXPLAIN QUERY PLAN {sql}",
                    params,
                ).fetchall()
                details.extend(str(row["detail"]) for row in rows)
            result[name] = details
        return result

    def table_counts(self) -> Mapping[str, int]:
        tables = (
            "source_instances",
            "source_conflicts",
            "sessions",
            "session_edges",
            "task_anchors",
            "facts",
            "work_claims",
            "usage_measurements",
            "usage_checkpoints",
            "cost_calculations",
            "migration_issues",
            "rm_task_current",
            "rm_usage_day",
        )
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


__all__ = [
    "APPLICATION_ID",
    "APP_VERSION_DEFAULT",
    "CanonicalRepository",
    "CanonicalStore",
    "MINIMUM_SQLITE_VERSION",
    "SCHEMA_BASE_NAME",
    "SCHEMA_MIGRATIONS",
    "SCHEMA_VERSION",
    "SchemaMigration",
    "canonical_hash",
    "now_us",
]
