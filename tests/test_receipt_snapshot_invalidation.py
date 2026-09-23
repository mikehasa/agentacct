"""Append-tolerant source generations must fail closed after destructive writes."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from agentacct import event_log
from agentacct.event_log import RawEventLog, serialize_event
from agentacct.evidence import EvidenceEnvelope, SubjectRefs
from agentacct.evidence_store import (
    EVIDENCE_STORE_SCHEMA_VERSION,
    EvidenceStore,
    _SNAPSHOT_UNIQUE_KEYS,
    read_evidence_snapshot_state,
)


def _event(event_id: str, **values: object) -> dict:
    return {"event_id": event_id, "event_type": "note", "created_at": 1.0, **values}


def _evidence(source_event_id: str, value: int = 1) -> EvidenceEnvelope:
    return EvidenceEnvelope.create(
        assertion="observed", event_type="tool_completed", source_type="client_hook",
        source_system="claude-code", source_instance="test", source_schema="test.v1",
        adapter="test.v1", source_event_id=source_event_id,
        event_timestamp="2026-09-23T00:00:00Z", dimensions=("tool_activity",),
        measurement_basis="client_hook_observed",
        subjects=SubjectRefs(client_session_id="session-1"), payload={"value": value},
    )


def test_event_append_preserves_safety_but_advances_capture_revision(tmp_path: Path) -> None:
    log = RawEventLog(tmp_path / "events.sqlite3")
    before = log.snapshot_state()
    log.append_event(_event("same-id"))
    log.append_event(_event("same-id", value="distinct row"))
    after = log.snapshot_state()
    assert after.database_id == before.database_id
    assert after.revision > before.revision
    assert after.destructive_revision == before.destructive_revision
    assert RawEventLog(log.db_path).snapshot_state() == after
    assert RawEventLog(tmp_path / "other.sqlite3").snapshot_state().database_id != after.database_id


def test_event_legacy_connection_runs_new_guards_without_recursive_triggers(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    legacy = sqlite3.connect(database)
    legacy.executescript("""
        CREATE TABLE event_lines (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT,
            run_id TEXT, event_type TEXT, created_at REAL, line TEXT NOT NULL);
        CREATE TABLE event_log_state (singleton INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
        INSERT INTO event_log_state VALUES (1, 42);
        INSERT INTO event_lines(event_id, line) VALUES ('old', '{}');
    """)
    log = RawEventLog(database)
    assert log.snapshot_state().revision == 42
    assert legacy.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    try:
        for statement in (
            "UPDATE event_lines SET line = '{\"redacted\":true}' WHERE seq = 1",
            "INSERT OR REPLACE INTO event_lines(seq, event_id, line) VALUES (1, 'replacement', '{}')",
            "DELETE FROM event_lines WHERE seq = 1",
        ):
            before = log.snapshot_state()
            legacy.execute(statement)
            legacy.commit()
            after = log.snapshot_state()
            assert after.revision > before.revision
            assert after.destructive_revision > before.destructive_revision
    finally:
        legacy.close()


def test_event_epoch_commits_atomically_and_rollback_does_not_invalidate(tmp_path: Path) -> None:
    log = RawEventLog(tmp_path / "events.sqlite3")
    log.append_event(_event("one"))
    before = log.snapshot_state()
    with sqlite3.connect(log.db_path) as writer:
        writer.execute("UPDATE event_lines SET line = '{}'")
        assert log.snapshot_state() == before
        writer.rollback()
        assert log.snapshot_state() == before
        writer.execute("DELETE FROM event_lines")
    after = log.snapshot_state()
    assert after.revision > before.revision
    assert after.destructive_revision > before.destructive_revision


def test_event_file_append_safe_and_redaction_reconcile_invalidates(tmp_path: Path) -> None:
    log = RawEventLog(tmp_path / "events.sqlite3")
    ledger = tmp_path / "events.jsonl"
    first = serialize_event(_event("one", note="private")) + "\n"
    ledger.write_text(first)
    log.reconcile_from_file(ledger)
    before = log.snapshot_state()
    ledger.write_text(first + serialize_event(_event("two")) + "\n")
    log.reconcile_from_file(ledger)
    assert log.snapshot_state().destructive_revision == before.destructive_revision
    ledger.write_text(serialize_event(_event("one", note="[redacted]")) + "\n")
    log.reconcile_from_file(ledger)
    assert log.snapshot_state().destructive_revision > before.destructive_revision


@pytest.mark.parametrize("kind", ["event", "evidence"])
def test_state_reads_do_not_initialize_write_or_read_source_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    store = RawEventLog(tmp_path / "events.sqlite3") if kind == "event" else EvidenceStore(tmp_path)
    expected = store.snapshot_state()
    real_connect = sqlite3.connect
    calls: list[tuple[object, dict]] = []

    def readonly_connect(database: object, **kwargs: object) -> sqlite3.Connection:
        calls.append((database, kwargs))
        connection = real_connect(database, **kwargs)

        def authorizer(action: int, table: str | None, *args: object) -> int:
            if action == sqlite3.SQLITE_READ and table not in {
                "event_log_snapshot_state", "event_log_state", "evidence_snapshot_state", "store_metadata",
                "evidence_mechanical_snapshot_state",
                "pragma_schema_version", "sqlite_master",
            }:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_PRAGMA and table == "schema_version" and args[0] is None:
                return sqlite3.SQLITE_OK
            if action not in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        return connection

    monkeypatch.setattr(event_log.sqlite3, "connect", readonly_connect)
    assert store.snapshot_state() == expected
    assert len(calls) == 1
    assert str(calls[0][0]).endswith("?mode=ro")
    assert calls[0][1] == {"uri": True, "timeout": 0.05}


@pytest.mark.parametrize("kind", ["event", "evidence"])
def test_missing_source_fails_closed_without_creating_file(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "absent.sqlite3"
    if kind == "event":
        log = object.__new__(RawEventLog)
        log.db_path = path
        read = log.snapshot_state
    else:
        read = lambda: read_evidence_snapshot_state(path)
    with pytest.raises(sqlite3.Error):
        read()
    assert not path.exists()


@pytest.mark.parametrize("kind", ["event", "evidence"])
@pytest.mark.parametrize("corruption", ["missing-row", "missing-table", "bad-id", "negative-epoch"])
def test_unknown_snapshot_state_fails_closed(tmp_path: Path, kind: str, corruption: str) -> None:
    store = RawEventLog(tmp_path / "events.sqlite3") if kind == "event" else EvidenceStore(tmp_path)
    path = store.db_path if kind == "event" else store.projection_path
    table = "event_log_snapshot_state" if kind == "event" else "evidence_snapshot_state"
    with sqlite3.connect(path) as connection:
        statement = {
            "missing-row": f"DELETE FROM {table}",
            "missing-table": f"DROP TABLE {table}",
            "bad-id": f"UPDATE {table} SET database_id = 'invalid'",
            "negative-epoch": f"UPDATE {table} SET destructive_revision = -1",
        }[corruption]
        connection.execute(statement)
    with pytest.raises(sqlite3.Error):
        store.snapshot_state()


def test_evidence_actual_append_duplicate_and_reopen_preserve_safety(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    before = store.snapshot_state()
    first = _evidence("first")
    store.append(first)
    store.append(first)
    store.append(_evidence("second"))
    after = store.snapshot_state()
    assert after.database_id == before.database_id
    assert after.revision > before.revision
    assert after.destructive_revision == before.destructive_revision
    assert EvidenceStore(tmp_path).snapshot_state() == after
    assert EvidenceStore(tmp_path / "other").snapshot_state().database_id != after.database_id


def test_evidence_conflict_append_immediately_invalidates_previous_snapshot(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.append(_evidence("conflict", 1))
    before = store.snapshot_state()
    store.append(_evidence("conflict", 2))
    assert store.snapshot_state().destructive_revision > before.destructive_revision


def test_evidence_legacy_connection_catches_delete_rewrite_and_both_receipt_unique_keys(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    first = store.append(_evidence("first"))
    legacy = sqlite3.connect(store.projection_path)
    assert legacy.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    try:
        # Simulate a legacy daemon connected before the snapshot schema exists.
        names = legacy.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%snapshot%'").fetchall()
        for (name,) in names:
            legacy.execute(f'DROP TRIGGER "{name}"')
        legacy.execute("DROP TABLE evidence_snapshot_state")
        legacy.commit()
        EvidenceStore(tmp_path)
        for statement, args in (
            ("UPDATE evidence_versions SET envelope_json = '{}' WHERE evidence_id = ?", (first.evidence_id,)),
            ("UPDATE evidence_versions SET first_receipt_sequence = 99 WHERE evidence_id = ?", (first.evidence_id,)),
            ("INSERT OR REPLACE INTO evidence_receipts SELECT sequence, 'different-receipt', spool_offset, evidence_id, idempotency_key, disposition, received_at FROM evidence_receipts LIMIT 1", ()),
            ("INSERT OR REPLACE INTO evidence_receipts SELECT sequence + 100, receipt_id, spool_offset, evidence_id, idempotency_key, disposition, received_at FROM evidence_receipts LIMIT 1", ()),
            ("DELETE FROM evidence_dimensions WHERE evidence_id = ?", (first.evidence_id,)),
            ("DELETE FROM evidence_receipts WHERE evidence_id = ?", (first.evidence_id,)),
            ("DELETE FROM evidence_versions WHERE evidence_id = ?", (first.evidence_id,)),
        ):
            before = store.snapshot_state()
            legacy.execute(statement, args)
            legacy.commit()
            after = store.snapshot_state()
            assert after.revision > before.revision
            assert after.destructive_revision > before.destructive_revision
    finally:
        legacy.close()


def test_evidence_noop_and_initial_first_receipt_do_not_invalidate(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    receipt = store.append(_evidence("first"))
    with sqlite3.connect(store.projection_path) as connection:
        connection.execute("UPDATE evidence_versions SET first_receipt_sequence = NULL")
    before = store.snapshot_state()
    with sqlite3.connect(store.projection_path) as connection:
        connection.execute("UPDATE evidence_versions SET first_receipt_sequence = ?", (receipt.receipt_sequence,))
        connection.execute("UPDATE evidence_versions SET is_conflict = is_conflict")
        connection.execute("INSERT INTO store_metadata VALUES ('replay_offset', '1234') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    after = store.snapshot_state()
    assert after.revision > before.revision
    assert after.destructive_revision == before.destructive_revision


def test_evidence_epoch_transaction_and_schema_identity(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.append(_evidence("first"))
    before = store.snapshot_state()
    with sqlite3.connect(store.projection_path) as connection:
        connection.execute("UPDATE evidence_versions SET envelope_json = '{}'")
        assert store.snapshot_state() == before
        connection.rollback()
        assert store.snapshot_state() == before
        connection.execute("UPDATE store_metadata SET value='unknown' WHERE key='schema_version'")
    with pytest.raises(sqlite3.DatabaseError):
        store.snapshot_state()
    with sqlite3.connect(store.projection_path) as connection:
        connection.execute("UPDATE store_metadata SET value=? WHERE key='schema_version'", (EVIDENCE_STORE_SCHEMA_VERSION,))
    assert store.snapshot_state().destructive_revision > before.destructive_revision


@pytest.mark.parametrize("table", list(_SNAPSHOT_UNIQUE_KEYS))
def test_evidence_replace_guards_use_index_lookups_not_projection_scans(tmp_path: Path, table: str) -> None:
    store = EvidenceStore(tmp_path)
    with sqlite3.connect(store.projection_path) as connection:
        for condition in ("rowid = NEW.rowid", *_SNAPSHOT_UNIQUE_KEYS[table]):
            columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            for column in sorted(["rowid", *columns], key=len, reverse=True):
                condition = condition.replace(f"NEW.{column}", "'current'" if column == "status" else "'test'")
            plan = connection.execute(f'EXPLAIN QUERY PLAN SELECT 1 FROM "{table}" WHERE {condition}').fetchall()
            assert all("SCAN " not in str(row[3]) for row in plan), plan
            assert any("SEARCH " in str(row[3]) for row in plan), plan


def _projection_row(connection: sqlite3.Connection, table: str, suffix: int) -> dict:
    """Raw legacy-writer fixture; avoid assuming today's Python write paths."""

    constrained = {
        "disposition": "inserted", "status": "current", "validation_state": "valid",
        "action": "insert", "complete": 0, "tombstoned": 0, "head_tombstoned": 0, "is_conflict": 0,
    }
    return {
        str(row[1]): constrained.get(str(row[1]), suffix if row[2] == "INTEGER" else f"{row[1]}-{suffix}")
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _insert_projection_row(connection: sqlite3.Connection, table: str, values: dict, *, replace: bool = False) -> None:
    columns = ", ".join(f'"{column}"' for column in values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f'INSERT {"OR REPLACE " if replace else ""}INTO "{table}" ({columns}) VALUES ({placeholders})',
        tuple(values.values()),
    )


@pytest.mark.parametrize("table, key", [
    (table, key) for table, keys in _SNAPSHOT_UNIQUE_KEYS.items() for key in keys
])
def test_evidence_replace_each_unique_key_invalidates_with_recursive_triggers_off(
    tmp_path: Path, table: str, key: str,
) -> None:
    store = EvidenceStore(tmp_path)
    with sqlite3.connect(store.projection_path) as legacy:
        assert legacy.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        first = _projection_row(legacy, table, 1)
        second = _projection_row(legacy, table, 2)
        for column in re.findall(r"(\w+) = NEW\.\w+", key):
            second[column] = first[column]
        _insert_projection_row(legacy, table, first)
        legacy.commit()
        before = store.snapshot_state()
        _insert_projection_row(legacy, table, second, replace=True)
    after = store.snapshot_state()
    assert after.destructive_revision > before.destructive_revision
    assert after.revision > before.revision


@pytest.mark.parametrize("table", list(_SNAPSHOT_UNIQUE_KEYS))
def test_evidence_update_delete_and_implicit_rowid_replace_are_guarded(tmp_path: Path, table: str) -> None:
    store = EvidenceStore(tmp_path)
    with sqlite3.connect(store.projection_path) as legacy:
        first = _projection_row(legacy, table, 1)
        _insert_projection_row(legacy, table, first)
        legacy.commit()
        before = store.snapshot_state()
        # Changing a substantive field, including refreshable head/status and
        # claimed-link validation, must change safety even in an old process.
        update_column = {
            "evidence_versions": "is_conflict",
            "claimed_link_versions": "validation_state",
            "refreshable_usage_revisions": "status",
            "refreshable_usage_heads": "content_hash",
        }.get(table, next(iter(first)))
        update_value = {"is_conflict": 1, "validation_state": "invalid", "status": "superseded"}.get(update_column, 100 if isinstance(first[update_column], int) else "changed")
        legacy.execute(f'UPDATE "{table}" SET "{update_column}" = ?', (update_value,))
        legacy.commit()
        after_update = store.snapshot_state()
        assert after_update.destructive_revision > before.destructive_revision

        if "sequence" not in first:  # sequence already aliases rowid otherwise
            replacement = {"rowid": 1, **_projection_row(legacy, table, 2)}
            _insert_projection_row(legacy, table, replacement, replace=True)
            legacy.commit()
            assert store.snapshot_state().destructive_revision > after_update.destructive_revision

        before_delete = store.snapshot_state()
        legacy.execute(f'DELETE FROM "{table}"')
    assert store.snapshot_state().destructive_revision > before_delete.destructive_revision


@pytest.mark.parametrize("kind", ["event", "evidence"])
def test_sqlite_backup_restore_into_same_inode_changes_schema_cookie(tmp_path: Path, kind: str) -> None:
    store = RawEventLog(tmp_path / "events.sqlite3") if kind == "event" else EvidenceStore(tmp_path)
    path = store.db_path if kind == "event" else store.projection_path

    def append(identity: str) -> None:
        store.append_event(_event(identity)) if kind == "event" else store.append(_evidence(identity))

    def backup(source: Path, target: Path) -> None:
        reader, writer = sqlite3.connect(source), sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            reader.close()
            writer.close()

    append("retained")
    backup(path, tmp_path / "backup.sqlite3")
    append("removed-by-restore")
    before = store.snapshot_state()
    inode = path.stat().st_ino
    backup(tmp_path / "backup.sqlite3", path)
    after = store.snapshot_state()
    assert path.stat().st_ino == inode
    assert after.database_id == before.database_id
    assert after.destructive_revision == before.destructive_revision
    assert after.revision < before.revision
    assert after.schema_cookie != before.schema_cookie


def test_mechanical_generation_ignores_real_refreshable_usage_updates(tmp_path: Path) -> None:
    from tests.test_evidence_store import _refreshable_usage_item

    store = EvidenceStore(tmp_path)
    store.append(_evidence("hook"))
    before = store.snapshot_state()
    store.reconcile_refreshable_usage((_refreshable_usage_item("slot", value=1, source_order=1),), complete=True)
    store.reconcile_refreshable_usage((_refreshable_usage_item("slot", value=2, source_order=2),), complete=True)
    store.reconcile_refreshable_usage((), complete=True)
    after = store.snapshot_state()
    assert after.revision > before.revision
    assert after.destructive_revision > before.destructive_revision
    assert after.mechanical_revision == before.mechanical_revision
    assert after.mechanical_destructive_revision == before.mechanical_destructive_revision
    assert after.schema_cookie == before.schema_cookie


def test_mechanical_hook_appends_and_duplicate_receipts_only_advance_revision(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    before = store.snapshot_state()
    hook = _evidence("hook")
    store.append(hook)
    store.append(hook)
    store.append(_evidence("independent-hook"))
    after = store.snapshot_state()
    assert after.mechanical_revision > before.mechanical_revision
    assert after.mechanical_destructive_revision == before.mechanical_destructive_revision
    store.append(_evidence("hook", 2))
    assert store.snapshot_state().mechanical_destructive_revision > after.mechanical_destructive_revision


@pytest.mark.parametrize("mutation", ["content", "source", "key", "delete", "replace"])
@pytest.mark.parametrize("target", ["hook", "mixed-member"])
def test_mechanical_version_mutations_follow_old_and_new_hook_group_membership(
    tmp_path: Path, mutation: str, target: str,
) -> None:
    store = EvidenceStore(tmp_path)
    receipt = store.append(_evidence("hook"))
    with sqlite3.connect(store.projection_path) as legacy:
        if target == "mixed-member":
            member = _projection_row(legacy, "evidence_versions", 10)
            member.update(evidence_id="mixed-member", idempotency_key=receipt.idempotency_key,
                          source_type="local_client_log")
            _insert_projection_row(legacy, "evidence_versions", member)
            evidence_id = "mixed-member"
        else:
            evidence_id = receipt.evidence_id
        legacy.commit()
        before = store.snapshot_state()
        statement = {
            "content": "UPDATE evidence_versions SET envelope_json='{}' WHERE evidence_id=?",
            "source": "UPDATE evidence_versions SET source_type='different' WHERE evidence_id=?",
            "key": "UPDATE evidence_versions SET idempotency_key='different' WHERE evidence_id=?",
            "delete": "DELETE FROM evidence_versions WHERE evidence_id=?",
            "replace": "INSERT OR REPLACE INTO evidence_versions SELECT * FROM evidence_versions WHERE evidence_id=?",
        }[mutation]
        legacy.execute(statement, (evidence_id,))
    after = store.snapshot_state()
    assert after.mechanical_revision > before.mechanical_revision
    assert after.mechanical_destructive_revision > before.mechanical_destructive_revision


def test_mechanical_mixed_group_insert_and_late_first_receipt_both_invalidate(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    hook = store.append(_evidence("hook"))
    before = store.snapshot_state()
    with sqlite3.connect(store.projection_path) as legacy:
        member = _projection_row(legacy, "evidence_versions", 10)
        member.update(evidence_id="mixed", idempotency_key=hook.idempotency_key, source_type="local_client_log")
        _insert_projection_row(legacy, "evidence_versions", member)
    inserted = store.snapshot_state()
    assert inserted.mechanical_destructive_revision > before.mechanical_destructive_revision
    # A committed version without receipts is excluded by the projection's
    # receipt JOIN. Making it visible later must invalidate captures in between.
    with sqlite3.connect(store.projection_path) as legacy:
        receipt = _projection_row(legacy, "evidence_receipts", 10)
        receipt.update(evidence_id="mixed", idempotency_key=hook.idempotency_key)
        _insert_projection_row(legacy, "evidence_receipts", receipt)
    assert store.snapshot_state().mechanical_destructive_revision > inserted.mechanical_destructive_revision


@pytest.mark.parametrize("mutation", ["update", "delete", "replace-sequence", "replace-receipt-id"])
def test_mechanical_receipt_mutations_guard_mixed_hook_groups(tmp_path: Path, mutation: str) -> None:
    store = EvidenceStore(tmp_path)
    hook = store.append(_evidence("hook"))
    with sqlite3.connect(store.projection_path) as legacy:
        member = _projection_row(legacy, "evidence_versions", 10)
        member.update(evidence_id="mixed", idempotency_key=hook.idempotency_key, source_type="local_client_log")
        _insert_projection_row(legacy, "evidence_versions", member)
        receipt = _projection_row(legacy, "evidence_receipts", 10)
        receipt.update(evidence_id="mixed", idempotency_key=hook.idempotency_key)
        _insert_projection_row(legacy, "evidence_receipts", receipt)
        legacy.commit()
        before = store.snapshot_state()
        if mutation == "update":
            legacy.execute("UPDATE evidence_receipts SET evidence_id='unrelated' WHERE sequence=10")
        elif mutation == "delete":
            legacy.execute("DELETE FROM evidence_receipts WHERE sequence=10")
        else:
            replacement = _projection_row(legacy, "evidence_receipts", 20)
            collision_key = "sequence" if mutation == "replace-sequence" else "receipt_id"
            replacement[collision_key] = receipt[collision_key]
            _insert_projection_row(legacy, "evidence_receipts", replacement, replace=True)
    assert store.snapshot_state().mechanical_destructive_revision > before.mechanical_destructive_revision


@pytest.mark.parametrize("key", ["rowid", "evidence_id"])
def test_update_or_replace_cannot_silently_delete_a_hook_with_nonhook_row(tmp_path: Path, key: str) -> None:
    store = EvidenceStore(tmp_path)
    hook = store.append(_evidence("hook"))
    with sqlite3.connect(store.projection_path) as legacy:
        member = _projection_row(legacy, "evidence_versions", 10)
        member.update(evidence_id="unrelated", source_type="local_client_log")
        _insert_projection_row(legacy, "evidence_versions", member)
        legacy.commit()
        before = store.snapshot_state()
        target = legacy.execute(f"SELECT {key} FROM evidence_versions WHERE evidence_id=?", (hook.evidence_id,)).fetchone()[0]
        assert legacy.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        legacy.execute(f"UPDATE OR REPLACE evidence_versions SET {key}=? WHERE evidence_id='unrelated'", (target,))
    after = store.snapshot_state()
    assert after.destructive_revision > before.destructive_revision
    assert after.mechanical_destructive_revision > before.mechanical_destructive_revision


def test_mechanical_epoch_migration_guards_an_already_open_legacy_connection(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    hook = store.append(_evidence("hook"))
    with sqlite3.connect(store.projection_path) as legacy:
        names = legacy.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%_mechanical_%'").fetchall()
        for (name,) in names:
            legacy.execute(f'DROP TRIGGER "{name}"')
        legacy.execute("DROP TABLE evidence_mechanical_snapshot_state")
        legacy.commit()
        with pytest.raises(sqlite3.Error):
            store.snapshot_state()
        EvidenceStore(tmp_path)
        before = store.snapshot_state()
        legacy.execute("DELETE FROM evidence_versions WHERE evidence_id=?", (hook.evidence_id,))
        legacy.commit()
        assert store.snapshot_state().mechanical_destructive_revision > before.mechanical_destructive_revision


def test_mechanical_group_membership_lookup_has_no_full_table_scan(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    with sqlite3.connect(store.projection_path) as connection:
        plan = connection.execute("""EXPLAIN QUERY PLAN
            SELECT 1 FROM evidence_versions AS member WHERE member.evidence_id = ?
                AND EXISTS (SELECT 1 FROM evidence_versions AS hook INDEXED BY idx_evidence_mechanical_group
                    WHERE hook.idempotency_key = member.idempotency_key AND hook.source_type = 'client_hook')
        """, ("example",)).fetchall()
    assert all("SCAN " not in str(row[3]) for row in plan), plan
    assert any("idx_evidence_mechanical_group" in str(row[3]) for row in plan), plan
