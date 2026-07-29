from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from agentacct.canonical import APPLICATION_ID, SCHEMA_VERSION, CanonicalStore


def test_candidate_schema_is_strict_versioned_and_owner_only(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    with CanonicalStore.create(database, store_uuid="00000000-0000-4000-8000-000000000001") as store:
        tables = store.connection.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type = 'table' ORDER BY name"
        ).fetchall()
        product_tables = [row for row in tables if not row["name"].startswith("sqlite_")]

        assert product_tables
        assert all(str(row["sql"]).rstrip().upper().endswith("STRICT") for row in product_tables)
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        assert store.connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert store.quick_check() == {
            "quick_check": ["ok"],
            "foreign_key_violations": [],
            "ok": True,
        }

        migration = store.connection.execute(
            "SELECT version, length(checksum), app_version FROM schema_migrations"
        ).fetchone()
        assert tuple(migration) == (SCHEMA_VERSION, 64, "sqlite-truth-spike")

        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO source_instances(client, adapter, representation, namespace_scheme, "
                "namespace_digest, created_at_us) VALUES ('codex', 'fixture', 'sqlite', "
                "'hmac-sha256-v1', x'01', 1)"
            )

    assert database.stat().st_mode & 0o777 == 0o600


def test_schema_has_no_raw_payload_or_generic_envelope_columns(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    forbidden_fragments = {
        "prompt",
        "response",
        "reasoning_body",
        "transcript",
        "tool_arguments",
        "tool_results",
        "stdout",
        "stderr",
        "envelope_json",
        "payload_json",
        "metadata_json",
    }
    with CanonicalStore.create(database) as store:
        table_names = [
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        columns = {
            str(row[1]).lower()
            for table in table_names
            for row in store.connection.execute(f"PRAGMA table_info({table})")
        }

    assert forbidden_fragments.isdisjoint(columns)


def test_scope_revision_constraints_reject_cross_scope_and_null_duplicates(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        connection = store.connection
        source_id = int(
            connection.execute(
                "INSERT INTO source_instances(client, adapter, representation, namespace_scheme, "
                "namespace_digest, created_at_us) VALUES (?, ?, ?, ?, ?, ?)",
                ("fixture", "fixture", "fixture", "fixture-v1", b"s" * 32, 1),
            ).lastrowid
        )
        fact_ids = tuple(
            int(
                connection.execute(
                    "INSERT INTO facts(source_instance_id, source_event_id, fact_type, transport, "
                    "strength, occurred_at_us, content_hash, created_at_us) "
                    "VALUES (?, ?, 'mechanical_observation', 'fixture', "
                    "'mechanical_observation', ?, ?, ?)",
                    (source_id, f"scope-fact-{index}", index, bytes([index]) * 32, index),
                ).lastrowid
            )
            for index in range(1, 4)
        )
        scope_ids = tuple(
            int(
                connection.execute(
                    "INSERT INTO claim_scopes(scope_key, scope_kind) VALUES (?, 'fixture')",
                    (f"scope-{index}",),
                ).lastrowid
            )
            for index in range(1, 3)
        )
        revision_ids = tuple(
            int(
                connection.execute(
                    "INSERT INTO scope_revisions(claim_scope_id, revision_key) VALUES (?, ?)",
                    (scope_id, f"revision-{index}"),
                ).lastrowid
            )
            for index, scope_id in enumerate(scope_ids, start=1)
        )

        cross_scope_writes = (
            (
                "INSERT INTO machine_checks(fact_id, claim_scope_id, scope_revision_id, "
                "check_identity, evidence_type, result) VALUES (?, ?, ?, 'fixture', 'fixture', 'passed')",
                (fact_ids[0], scope_ids[0], revision_ids[1]),
            ),
            (
                "INSERT INTO terminal_requirements(terminal_fact_id, claim_scope_id, scope_revision_id) "
                "VALUES (?, ?, ?)",
                (fact_ids[1], scope_ids[0], revision_ids[1]),
            ),
            (
                "INSERT INTO evidence_support_links(fact_id, claim_scope_id, scope_revision_id, support_kind) "
                "VALUES (?, ?, ?, 'supports')",
                (fact_ids[2], scope_ids[0], revision_ids[1]),
            ),
        )
        for sql, parameters in cross_scope_writes:
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                connection.execute(sql, parameters)

        connection.execute(
            "INSERT INTO machine_checks(fact_id, claim_scope_id, scope_revision_id, "
            "check_identity, evidence_type, result) VALUES (?, ?, ?, 'fixture', 'fixture', 'passed')",
            (fact_ids[0], scope_ids[0], revision_ids[0]),
        )
        connection.execute(
            "INSERT INTO terminal_requirements(terminal_fact_id, claim_scope_id, scope_revision_id) "
            "VALUES (?, ?, ?)",
            (fact_ids[1], scope_ids[0], revision_ids[0]),
        )
        connection.execute(
            "INSERT INTO evidence_support_links(fact_id, claim_scope_id, scope_revision_id, support_kind) "
            "VALUES (?, ?, NULL, 'supports')",
            (fact_ids[2], scope_ids[0]),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO evidence_support_links(fact_id, claim_scope_id, scope_revision_id, support_kind) "
                "VALUES (?, ?, NULL, 'supports')",
                (fact_ids[2], scope_ids[0]),
            )
        connection.execute(
            "INSERT INTO evidence_support_links(fact_id, claim_scope_id, scope_revision_id, support_kind) "
            "VALUES (?, ?, NULL, 'contradicts')",
            (fact_ids[2], scope_ids[0]),
        )
        connection.execute(
            "INSERT INTO evidence_support_links(fact_id, claim_scope_id, scope_revision_id, support_kind) "
            "VALUES (?, ?, ?, 'supports')",
            (fact_ids[2], scope_ids[0], revision_ids[0]),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO evidence_support_links(fact_id, claim_scope_id, scope_revision_id, support_kind) "
                "VALUES (?, ?, ?, 'supports')",
                (fact_ids[2], scope_ids[0], revision_ids[0]),
            )
        connection.execute(
            "INSERT INTO evidence_support_links(fact_id, claim_scope_id, scope_revision_id, support_kind) "
            "VALUES (?, ?, ?, 'contradicts')",
            (fact_ids[2], scope_ids[0], revision_ids[0]),
        )

        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT scope_revision_id, support_kind FROM evidence_support_links "
                "WHERE fact_id = ? AND claim_scope_id = ? "
                "ORDER BY scope_revision_id IS NOT NULL, support_kind",
                (fact_ids[2], scope_ids[0]),
            ).fetchall()
        ] == [
            (None, "contradicts"),
            (None, "supports"),
            (revision_ids[0], "contradicts"),
            (revision_ids[0], "supports"),
        ]


def test_candidate_paths_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        CanonicalStore.create("relative.sqlite3")

    live_shaped = tmp_path / ".agent-sentinel" / "state" / "candidate.sqlite3"
    with pytest.raises(ValueError, match="cannot be created"):
        CanonicalStore.create(live_shaped)
    assert not live_shaped.exists()

    global_live = tmp_path / ".agent-sentinel-global" / "state"
    global_live.mkdir(parents=True, mode=0o700)
    with pytest.raises(ValueError, match="live agentacct"):
        CanonicalStore.create(global_live / "candidate.sqlite3")

    codex_live = tmp_path / ".codex"
    codex_live.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="live Codex"):
        CanonicalStore.create(codex_live / "candidate.sqlite3")

    configured_live = tmp_path / "custom-codex-home"
    configured_live.mkdir(mode=0o700)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("CODEX_HOME", str(configured_live))
        with pytest.raises(ValueError, match="configured live Codex"):
            CanonicalStore.create(configured_live / "candidate.sqlite3")

    missing_parent = tmp_path / "does-not-exist" / "candidate.sqlite3"
    with pytest.raises(ValueError, match="does not exist"):
        CanonicalStore.create(missing_parent)
    assert not missing_parent.parent.exists()

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        CanonicalStore.create(linked_parent / "candidate.sqlite3")

    permissive_parent = tmp_path / "permissive-parent"
    permissive_parent.mkdir(mode=0o755)
    permissive_parent.chmod(0o755)
    with pytest.raises(PermissionError, match="owner-only"):
        CanonicalStore.create(permissive_parent / "candidate.sqlite3")


@pytest.mark.parametrize(
    "variable",
    ("AGENT_CHRONICLE_STORE_DIR", "AGENT_SENTINEL_STORE_DIR"),
)
@pytest.mark.parametrize("relation", ("exact", "descendant", "ancestor"))
def test_candidate_rejects_every_configured_live_store_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    relation: str,
) -> None:
    if relation == "descendant":
        live_root = tmp_path / "configured-live-store"
        live_root.mkdir(mode=0o700)
        database = live_root / "candidate.sqlite3"
    else:
        database = tmp_path / "candidate.sqlite3"
        live_root = database if relation == "exact" else database / "nested-live-store"
    monkeypatch.setenv(variable, str(live_root))

    with pytest.raises(ValueError, match="disjoint.*configured live state root"):
        CanonicalStore.create(database)

    assert not database.exists()


def test_create_rejects_redirected_parent_resolution_before_creating_any_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_parent = tmp_path / "safe-parent"
    safe_parent.mkdir(mode=0o700)
    live_parent = tmp_path / ".codex"
    live_parent.mkdir(mode=0o700)
    database = safe_parent / "candidate.sqlite3"
    live_database = live_parent / database.name
    real_resolve = Path.resolve

    def redirected_resolve(self: Path, strict: bool = False) -> Path:
        if self == safe_parent and strict:
            return live_parent
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)

    with pytest.raises(ValueError, match="live Codex"):
        CanonicalStore.create(database)

    assert not database.exists()
    assert not live_database.exists()


def test_create_openat_does_not_follow_parent_exchanged_for_live_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_parent = tmp_path / "safe-parent"
    safe_parent.mkdir(mode=0o700)
    moved_safe_parent = tmp_path / "moved-safe-parent"
    live_parent = tmp_path / ".codex"
    live_parent.mkdir(mode=0o700)
    database = safe_parent / "candidate.sqlite3"
    real_open = os.open
    armed = True

    def racing_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal armed
        if armed and path == database.name and dir_fd is not None:
            armed = False
            safe_parent.rename(moved_safe_parent)
            safe_parent.symlink_to(live_parent, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)

    with pytest.raises(ValueError, match="symlink"):
        CanonicalStore.create(database)

    assert not (live_parent / database.name).exists()
    assert (moved_safe_parent / database.name).is_file()


def test_newer_or_foreign_schema_is_not_opened(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    store = CanonicalStore.create(database)
    store.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    store.close()

    with pytest.raises(ValueError, match="newer than this build supports"):
        CanonicalStore.open(database)

    foreign = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE unrelated(value TEXT) STRICT")
    connection.close()
    foreign.chmod(0o600)
    with pytest.raises(ValueError, match="not a canonical candidate database"):
        CanonicalStore.open(foreign)


def test_open_rejects_tampered_schema_migration_checksum(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    store = CanonicalStore.create(database)
    store.connection.execute(
        "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
        ("0" * 64, SCHEMA_VERSION),
    )
    store.close()

    with pytest.raises(ValueError, match="schema migration metadata"):
        CanonicalStore.open(database)


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "DROP INDEX idx_rm_task_work_recent",
        "DROP TABLE rm_usage_day",
    ),
    ids=("dropped-index", "dropped-table"),
)
def test_open_rejects_actual_schema_ddl_drift(
    tmp_path: Path,
    tamper_sql: str,
) -> None:
    database = tmp_path / "candidate.sqlite3"
    store = CanonicalStore.create(database)
    store.connection.execute(tamper_sql)
    store.close()

    with pytest.raises(ValueError, match="schema DDL is incompatible"):
        CanonicalStore.open(database, read_only=True)


def test_open_rejects_symlink_alias(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    alias = tmp_path / "candidate-alias.sqlite3"
    alias.symlink_to(database)

    with pytest.raises(ValueError, match="symlink"):
        CanonicalStore.open(alias, read_only=True)


def test_open_rejects_non_owner_only_candidate(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    database.chmod(0o644)

    with pytest.raises(PermissionError, match=r"owner-only \(0600\)"):
        CanonicalStore.open(database, read_only=True)


def test_open_rejects_non_owner_only_parent_directory(tmp_path: Path) -> None:
    parent = tmp_path / "candidate-parent"
    parent.mkdir(mode=0o700)
    database = parent / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    parent.chmod(0o755)
    try:
        with pytest.raises(PermissionError, match="parent must be owner-only"):
            CanonicalStore.open(database, read_only=True)
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_open_rejects_non_owner_only_existing_sidecar(
    tmp_path: Path,
    suffix: str,
) -> None:
    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    sidecar = Path(f"{database}{suffix}")
    sidecar.write_bytes(b"unsafe-sidecar")
    sidecar.chmod(0o644)

    with pytest.raises(PermissionError, match=r"sidecar must be owner-only \(0600\)"):
        CanonicalStore.open(database, read_only=True)


def test_transaction_rolls_back_base_exception(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        before = store.connection.execute(
            "SELECT canonical_sequence FROM store_metadata WHERE singleton = 1"
        ).fetchone()[0]

        with pytest.raises(KeyboardInterrupt):
            with store.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE store_metadata SET canonical_sequence = ? WHERE singleton = 1",
                    (before + 99,),
                )
                raise KeyboardInterrupt

        assert store.connection.in_transaction is False
        assert store.connection.execute(
            "SELECT canonical_sequence FROM store_metadata WHERE singleton = 1"
        ).fetchone()[0] == before
        with store.transaction(write=True) as connection:
            connection.execute(
                "UPDATE store_metadata SET canonical_sequence = canonical_sequence + 1 "
                "WHERE singleton = 1"
            )


def test_transaction_failure_after_auto_rollback_propagates_original_error(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        with pytest.raises(RuntimeError, match="candidate write failed: root cause"):
            with store.transaction(write=True) as connection:
                # SQLITE_FULL/IOERR/NOMEM end the transaction before the error
                # surfaces; a bare ROLLBACK afterwards raises "cannot rollback".
                connection.execute("ROLLBACK")
                raise RuntimeError("candidate write failed: root cause")

        assert store.connection.in_transaction is False
        with store.transaction(write=True) as connection:
            connection.execute(
                "UPDATE store_metadata SET canonical_sequence = canonical_sequence + 1 "
                "WHERE singleton = 1"
            )


def test_transaction_commit_failure_rolls_back_uncommitted_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CommitFailingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            if sql == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return self._connection.execute(sql, *args)

    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        real_connection = store.connection
        before = real_connection.execute(
            "SELECT canonical_sequence FROM store_metadata WHERE singleton = 1"
        ).fetchone()[0]
        monkeypatch.setattr(store, "connection", CommitFailingConnection(real_connection))

        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            with store.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE store_metadata SET canonical_sequence = ? WHERE singleton = 1",
                    (before + 99,),
                )

        assert real_connection.in_transaction is False
        assert real_connection.execute(
            "SELECT canonical_sequence FROM store_metadata WHERE singleton = 1"
        ).fetchone()[0] == before


def test_open_rejects_candidate_replaced_during_sqlite_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.sqlite3"
    opened_inode = tmp_path / "opened-inode.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    CanonicalStore.create(database).close()
    CanonicalStore.create(replacement).close()
    real_connect = sqlite3.connect
    armed = True

    def racing_connect(path: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal armed
        connection = real_connect(path, *args, **kwargs)
        if armed and path != ":memory:":
            armed = False
            database.replace(opened_inode)
            replacement.replace(database)
        return connection

    monkeypatch.setattr(sqlite3, "connect", racing_connect)

    with pytest.raises(RuntimeError, match="path changed"):
        CanonicalStore.open(database, read_only=True)
    assert database.exists()
    assert opened_inode.exists()


def test_open_rejects_candidate_resolved_into_forbidden_live_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.sqlite3"
    forbidden_root = tmp_path / ".codex"
    forbidden_root.mkdir()
    moved_database = forbidden_root / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    original_bytes = database.read_bytes()
    real_resolve = Path.resolve
    armed = True

    def racing_resolve(self: Path, strict: bool = False) -> Path:
        nonlocal armed
        if armed and self == database and strict:
            armed = False
            database.replace(moved_database)
            database.symlink_to(moved_database)
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", racing_resolve)

    with pytest.raises(ValueError, match="live Codex"):
        CanonicalStore.open(database, read_only=False)

    assert database.is_symlink()
    assert moved_database.read_bytes() == original_bytes


def test_create_rejects_candidate_replaced_before_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.sqlite3"
    opened_inode = tmp_path / "opened-inode.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(b"replacement")
    replacement_identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    real_connect = sqlite3.connect

    def racing_connect(path: Path, *args: object, **kwargs: object) -> sqlite3.Connection:
        database.replace(opened_inode)
        replacement.replace(database)
        return real_connect(path, *args, **kwargs)

    def forbidden_unlink(*args: object, **kwargs: object) -> None:
        raise AssertionError("create failure cleanup must not unlink by path")

    monkeypatch.setattr(sqlite3, "connect", racing_connect)
    monkeypatch.setattr(Path, "unlink", forbidden_unlink)

    with pytest.raises(RuntimeError, match="path changed"):
        CanonicalStore.create(database)
    assert opened_inode.exists()
    assert database.exists()
    assert (database.stat().st_dev, database.stat().st_ino) == replacement_identity
    assert database.read_bytes() == b"replacement"


def test_create_rejects_candidate_replaced_during_descriptor_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.sqlite3"
    opened_inode = tmp_path / "opened-inode.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    CanonicalStore.create(replacement).close()
    replacement_identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    real_fchmod = os.fchmod
    armed = True

    def racing_fchmod(descriptor: int, mode: int) -> None:
        nonlocal armed
        if armed:
            armed = False
            database.replace(opened_inode)
            replacement.replace(database)
        real_fchmod(descriptor, mode)

    def forbidden_unlink(*args: object, **kwargs: object) -> None:
        raise AssertionError("create failure cleanup must not unlink by path")

    monkeypatch.setattr(os, "fchmod", racing_fchmod)
    monkeypatch.setattr(Path, "unlink", forbidden_unlink)

    with pytest.raises(RuntimeError, match="path changed"):
        CanonicalStore.create(database)

    assert opened_inode.exists()
    assert database.exists()
    assert (database.stat().st_dev, database.stat().st_ino) == replacement_identity


def _insert_issue(connection: sqlite3.Connection, reason: str) -> None:
    connection.execute(
        "INSERT INTO migration_issues(legacy_origin, location_digest, reason, disposition, count, first_seen_at_us) "
        "VALUES ('events.jsonl', ?, ?, 'invalid', 1, 1)",
        (reason.encode("utf-8").ljust(32, b"\x00")[:32], reason),
    )


def _issue_reasons(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT reason FROM migration_issues")
    }


def test_nested_transaction_scopes_are_savepoints(tmp_path: Path) -> None:
    store = CanonicalStore.create((tmp_path / "savepoints.sqlite3").resolve())
    try:
        with store.transaction(write=True) as connection:
            _insert_issue(connection, "outer")
            with store.transaction(write=True) as nested:
                _insert_issue(nested, "inner-released")
            with pytest.raises(ValueError, match="inner scope failure"):
                with store.transaction(write=True) as nested:
                    _insert_issue(nested, "inner-rolled-back")
                    raise ValueError("inner scope failure")
            assert store.connection.in_transaction

        assert store.connection.in_transaction is False
        assert _issue_reasons(store.connection) == {"outer", "inner-released"}
    finally:
        store.close()


def test_outer_abort_discards_released_nested_scopes(tmp_path: Path) -> None:
    store = CanonicalStore.create((tmp_path / "outer-abort.sqlite3").resolve())
    try:
        with pytest.raises(RuntimeError, match="outer scope failure"):
            with store.transaction(write=True) as connection:
                _insert_issue(connection, "outer")
                with store.transaction(write=True) as nested:
                    _insert_issue(nested, "inner-released")
                raise RuntimeError("outer scope failure")

        assert store.connection.in_transaction is False
        assert _issue_reasons(store.connection) == set()
    finally:
        store.close()


def test_lost_outer_transaction_fails_loudly_instead_of_committing(
    tmp_path: Path,
) -> None:
    store = CanonicalStore.create((tmp_path / "lost-outer.sqlite3").resolve())
    try:
        with pytest.raises(RuntimeError, match="atomic scope cannot continue"):
            with store.transaction(write=True) as connection:
                _insert_issue(connection, "outer")
                # Simulates SQLITE_FULL/IOERR auto-rollback whose error a
                # caller swallowed: the scope is open but the transaction died.
                connection.execute("ROLLBACK")
                with store.transaction(write=True):
                    raise AssertionError("nested scope must not begin durably")

        assert store.connection.in_transaction is False
        assert _issue_reasons(store.connection) == set()
    finally:
        store.close()


def _downgrade_to_version_one(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA user_version = 1")
        connection.execute("UPDATE store_metadata SET schema_version = 1")
        connection.execute(
            "UPDATE schema_migrations SET version = 1 WHERE version = ?",
            (SCHEMA_VERSION,),
        )
        connection.commit()
    finally:
        connection.close()


def test_older_schema_without_upgrade_path_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    _downgrade_to_version_one(database)

    with pytest.raises(ValueError, match="no upgrade path"):
        CanonicalStore.open(database)


def test_read_only_open_refuses_older_schema(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    _downgrade_to_version_one(database)

    with pytest.raises(ValueError, match="open writable to upgrade"):
        CanonicalStore.open(database, read_only=True)


def test_registered_migration_upgrades_stamps_and_reopens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentacct.canonical import sqlite as canonical_sqlite

    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()

    step = canonical_sqlite.SchemaMigration(
        from_version=SCHEMA_VERSION,
        name="test-versioning-only-step",
        statements=(),
    )
    monkeypatch.setattr(canonical_sqlite, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setitem(canonical_sqlite.SCHEMA_MIGRATIONS, SCHEMA_VERSION, step)

    store = CanonicalStore.open(database)
    try:
        assert (
            store.connection.execute("PRAGMA user_version").fetchone()[0]
            == SCHEMA_VERSION + 1
        )
        rows = [
            tuple(row)
            for row in store.connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            )
        ]
        assert rows[0][0] == SCHEMA_VERSION
        assert rows[1] == (SCHEMA_VERSION + 1, step.name, step.checksum)
        metadata = store.connection.execute(
            "SELECT schema_version FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        assert int(metadata[0]) == SCHEMA_VERSION + 1
    finally:
        store.close()

    reopened = CanonicalStore.open(database, read_only=True)
    try:
        assert (
            reopened.connection.execute("PRAGMA user_version").fetchone()[0]
            == SCHEMA_VERSION + 1
        )
    finally:
        reopened.close()


def test_failed_migration_step_rolls_back_and_store_stays_openable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentacct.canonical import sqlite as canonical_sqlite

    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()

    broken = canonical_sqlite.SchemaMigration(
        from_version=SCHEMA_VERSION,
        name="test-broken-step",
        statements=("CREATE TABLE definitely broken syntax",),
    )
    monkeypatch.setattr(canonical_sqlite, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setitem(canonical_sqlite.SCHEMA_MIGRATIONS, SCHEMA_VERSION, broken)

    with pytest.raises(ValueError, match="schema migration 'test-broken-step' to version"):
        CanonicalStore.open(database)

    monkeypatch.undo()
    store = CanonicalStore.open(database)
    try:
        assert (
            store.connection.execute("PRAGMA user_version").fetchone()[0]
            == SCHEMA_VERSION
        )
        rows = store.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()
        assert int(rows[0]) == 1
    finally:
        store.close()


def test_migration_statement_ending_the_transaction_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentacct.canonical import sqlite as canonical_sqlite

    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()

    rogue = canonical_sqlite.SchemaMigration(
        from_version=SCHEMA_VERSION,
        name="test-rogue-commit-step",
        statements=("COMMIT",),
    )
    monkeypatch.setattr(canonical_sqlite, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setitem(canonical_sqlite.SCHEMA_MIGRATIONS, SCHEMA_VERSION, rogue)

    with pytest.raises(ValueError, match="ended the transaction"):
        CanonicalStore.open(database)

    monkeypatch.undo()
    store = CanonicalStore.open(database)
    try:
        assert (
            store.connection.execute("PRAGMA user_version").fetchone()[0]
            == SCHEMA_VERSION
        )
    finally:
        store.close()


def test_mis_keyed_migration_step_is_refused_before_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentacct.canonical import sqlite as canonical_sqlite

    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()

    mis_keyed = canonical_sqlite.SchemaMigration(
        from_version=SCHEMA_VERSION + 5,
        name="test-mis-keyed-step",
        statements=(),
    )
    monkeypatch.setattr(canonical_sqlite, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setitem(canonical_sqlite.SCHEMA_MIGRATIONS, SCHEMA_VERSION, mis_keyed)

    with pytest.raises(ValueError, match="declares from_version"):
        CanonicalStore.open(database)


def test_deleted_intermediate_migration_row_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentacct.canonical import sqlite as canonical_sqlite

    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()

    steps = {
        SCHEMA_VERSION: canonical_sqlite.SchemaMigration(
            from_version=SCHEMA_VERSION, name="test-step-a", statements=()
        ),
        SCHEMA_VERSION + 1: canonical_sqlite.SchemaMigration(
            from_version=SCHEMA_VERSION + 1, name="test-step-b", statements=()
        ),
    }
    monkeypatch.setattr(canonical_sqlite, "SCHEMA_VERSION", SCHEMA_VERSION + 2)
    for key, value in steps.items():
        monkeypatch.setitem(canonical_sqlite.SCHEMA_MIGRATIONS, key, value)

    CanonicalStore.open(database).close()

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (SCHEMA_VERSION + 1,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="schema migration metadata"):
        CanonicalStore.open(database)


def _downgrade_to_version_two(database: Path, *, birth_checksum: str | None = None) -> None:
    """Rebuild a genuine v2 store: v2 store_metadata DDL, v2 stamps, v2 base row."""

    from agentacct.canonical.sqlite import HISTORICAL_SCHEMA_CHECKSUMS

    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.executescript(
            """
            DROP TABLE session_observed_lineage;
            CREATE TABLE store_metadata_v2_test AS
                SELECT singleton, store_uuid, store_role, schema_version,
                       canonical_sequence, created_at_us FROM store_metadata;
            DROP TABLE store_metadata;
            CREATE TABLE store_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                store_uuid TEXT NOT NULL UNIQUE,
                store_role TEXT NOT NULL CHECK (store_role = 'candidate'),
                schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
                canonical_sequence INTEGER NOT NULL DEFAULT 0 CHECK (canonical_sequence >= 0),
                created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0)
            ) STRICT;
            INSERT INTO store_metadata
                SELECT singleton, store_uuid, store_role, 2,
                       canonical_sequence, created_at_us FROM store_metadata_v2_test;
            DROP TABLE store_metadata_v2_test;
            PRAGMA user_version = 2;
            """
        )
        connection.execute(
            "UPDATE schema_migrations SET version = 2, name = 'canonical-v2', "
            "checksum = ? WHERE version = ?",
            (birth_checksum or HISTORICAL_SCHEMA_CHECKSUMS[2][1], SCHEMA_VERSION),
        )
    finally:
        connection.close()


def test_v2_store_upgrades_to_v3_on_writable_open(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    created = CanonicalStore.create(database)
    original_uuid = created.connection.execute(
        "SELECT store_uuid FROM store_metadata"
    ).fetchone()[0]
    created.close()
    _downgrade_to_version_two(database)

    store = CanonicalStore.open(database)
    try:
        metadata = store.connection.execute(
            "SELECT store_uuid, store_role, schema_version FROM store_metadata"
        ).fetchone()
        assert tuple(metadata) == (original_uuid, "candidate", SCHEMA_VERSION)
        rows = store.connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (2, "canonical-v2"),
            (3, "allow-live-store-role"),
            (4, "add-session-observed-lineage"),
        ]
        assert (
            store.connection.execute("PRAGMA user_version").fetchone()[0]
            == SCHEMA_VERSION
        )
    finally:
        store.close()
    # The migrated inventory must satisfy a fresh fail-closed reopen.
    CanonicalStore.open(database).close()


def test_v2_store_read_only_open_refuses_with_upgrade_guidance(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    _downgrade_to_version_two(database)

    with pytest.raises(ValueError, match="schema version 2 predates"):
        CanonicalStore.open(database, read_only=True)


def test_v2_birth_row_tamper_is_refused_after_migration_support(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    _downgrade_to_version_two(database, birth_checksum="ab" * 32)

    with pytest.raises(ValueError, match="schema migration metadata"):
        CanonicalStore.open(database)


def test_stale_migrator_adopts_concurrently_finished_chain(tmp_path: Path) -> None:
    """A racing writable open that lost the migration must adopt the winner's
    committed progress instead of failing on the stamping UNIQUE constraint."""

    from agentacct.canonical import sqlite as canonical_sqlite

    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    _downgrade_to_version_two(database)
    # Winner migrates.
    CanonicalStore.open(database).close()
    # Loser still believes from_version=2 (its pre-lock read was stale).
    connection = sqlite3.connect(database, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        result = canonical_sqlite._apply_schema_migrations(
            connection,
            from_version=2,
            app_version=canonical_sqlite.APP_VERSION_DEFAULT,
        )
        assert result == SCHEMA_VERSION
        rows = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
        assert int(rows[0]) == 1
    finally:
        connection.close()


def test_migration_lock_timeout_is_diagnosed_not_misfiled(tmp_path: Path) -> None:
    from agentacct.canonical import sqlite as canonical_sqlite

    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    _downgrade_to_version_two(database)

    holder = sqlite3.connect(database, isolation_level=None)
    migrator = sqlite3.connect(database, isolation_level=None)
    migrator.row_factory = sqlite3.Row
    try:
        holder.execute("BEGIN IMMEDIATE")
        migrator.execute("PRAGMA busy_timeout = 100")
        with pytest.raises(ValueError, match="could not acquire the write lock"):
            canonical_sqlite._apply_schema_migrations(
                migrator,
                from_version=2,
                app_version=canonical_sqlite.APP_VERSION_DEFAULT,
            )
    finally:
        holder.close()
        migrator.close()


def _downgrade_to_version_three(database: Path) -> None:
    """Rebuild a genuine v3 store: drop the v4 table, restamp, v3 base row."""

    from agentacct.canonical.sqlite import HISTORICAL_SCHEMA_CHECKSUMS

    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.executescript(
            """
            DROP TABLE session_observed_lineage;
            PRAGMA user_version = 3;
            """
        )
        connection.execute("UPDATE store_metadata SET schema_version = 3")
        connection.execute(
            "UPDATE schema_migrations SET version = 3, name = 'canonical-v3', "
            "checksum = ? WHERE version = ?",
            (HISTORICAL_SCHEMA_CHECKSUMS[3][1], SCHEMA_VERSION),
        )
    finally:
        connection.close()


def test_v3_store_migrates_to_v4_on_writable_open(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    _downgrade_to_version_three(database)

    store = CanonicalStore.open(database)
    try:
        rows = store.connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (3, "canonical-v3"),
            (4, "add-session-observed-lineage"),
        ]
        assert (
            int(store.connection.execute("SELECT COUNT(*) FROM session_observed_lineage").fetchone()[0])
            == 0
        )
    finally:
        store.close()
    CanonicalStore.open(database).close()


def test_v3_store_read_only_open_refuses(tmp_path: Path) -> None:
    database = tmp_path / "candidate.sqlite3"
    CanonicalStore.create(database).close()
    _downgrade_to_version_three(database)

    with pytest.raises(ValueError, match="schema version 3 predates"):
        CanonicalStore.open(database, read_only=True)
