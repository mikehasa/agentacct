from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from agentacct.receipt_snapshot_store import ReceiptSnapshotStore


def publish(store, token="one", **kwargs):
    return store.publish({("receipt", "a"): {"token": token}}, input_token=token, safety_token="safe", **kwargs)


def test_reads_do_not_initialize_missing_store(tmp_path):
    store = ReceiptSnapshotStore(tmp_path / "absent")
    assert store.read("receipt", "a") is None
    assert store.generation() is None
    assert store.clear() is False
    assert not store.store_dir.exists()


def test_generation_survives_reopen_and_returns_independent_payload(tmp_path):
    store = ReceiptSnapshotStore(tmp_path)
    generation = publish(store, built_at=100.0, cpu_seconds=2.0)
    reopened = ReceiptSnapshotStore(tmp_path)
    entry = reopened.read("receipt", "a")
    assert entry.generation == generation
    assert entry.input_token == "one" and entry.safety_token == "safe"
    assert entry.built_at == 100.0 and entry.generation_id == generation.generation_id
    entry.payload["token"] = "mutated"
    assert reopened.read("receipt", "a").payload == {"token": "one"}
    assert reopened.read("receipt", "unknown") is None
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700


def test_current_generation_has_no_old_entries_or_history(tmp_path):
    store = ReceiptSnapshotStore(tmp_path)
    publish(store)
    next_generation = store.publish({("tasks", "all"): {"tasks": []}}, input_token="two", safety_token="safe")
    assert store.read("receipt", "a") is None
    assert store.read("tasks", "all").generation_id == next_generation.generation_id
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM snapshot_generation").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM snapshot_entries").fetchone()[0] == 1


def test_unchanged_payload_does_not_write_entry_again(tmp_path):
    store = ReceiptSnapshotStore(tmp_path)
    first = publish(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER forbid_entry_update BEFORE UPDATE ON snapshot_entries "
                           "BEGIN SELECT RAISE(ABORT,'unchanged should not write'); END")
    second = publish(store)
    assert second.generation_id != first.generation_id
    assert store.read("receipt", "a").generation_id == second.generation_id


def test_failed_mid_publication_rolls_back_all_payloads_and_generation(tmp_path):
    store = ReceiptSnapshotStore(tmp_path)
    first = publish(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON snapshot_generation "
                           "BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.publish({("receipt", "b"): {"new": True}}, input_token="two", safety_token="safe")
    assert store.generation() == first
    assert store.read("receipt", "a").payload == {"token": "one"}
    assert store.read("receipt", "b") is None


@pytest.mark.parametrize("limits,entries", [
    ({"max_entries": 1}, {("r", "a"): {}, ("r", "b"): {}}),
    ({"max_entry_bytes": 25}, {("r", "a"): {"x": "a" * 30}}),
    ({"max_generation_bytes": 25}, {("r", "a"): {"a": "a" * 10}, ("r", "b"): {"a": "a" * 10}}),
    ({}, {("r", "a"): {"n": float("nan")}}),
])
def test_limits_and_invalid_json_preserve_previous_generation(tmp_path, limits, entries):
    store = ReceiptSnapshotStore(tmp_path)
    first = publish(store)
    bounded = ReceiptSnapshotStore(tmp_path, **limits)
    with pytest.raises(ValueError):
        bounded.publish(entries, input_token="two", safety_token="safe")
    assert store.generation() == first


def test_store_identity_and_projector_versions_refuse_incompatible_cache(tmp_path):
    store = ReceiptSnapshotStore(tmp_path / "first", projector_version="first")
    publish(store)
    assert ReceiptSnapshotStore(store.store_dir, projector_version="second").read("receipt", "a") is None
    copied = ReceiptSnapshotStore(tmp_path / "second", projector_version="first")
    copied.root.mkdir(parents=True)
    shutil.copy2(store.path, copied.path)
    assert copied.generation() is None
    assert copied.read("receipt", "a") is None


def test_new_package_release_rejects_previous_release_snapshot(tmp_path, monkeypatch):
    import agentacct.receipt_snapshot_store as current
    import agentacct.version as version

    store = ReceiptSnapshotStore(tmp_path)
    generation = publish(store)
    assert f"agentacct:{version.package_version()}:" in generation.projector_version
    monkeypatch.setattr(version, "package_version", lambda: "999.0.0-test-release")
    # Load the upgraded module independently, without disturbing the runtime's
    # imported classes/constants for other tests in this interpreter.
    name = "agentacct._test_upgraded_receipt_snapshot_store"
    spec = importlib.util.spec_from_file_location(name, current.__file__)
    assert spec is not None and spec.loader is not None
    upgraded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, upgraded)
    spec.loader.exec_module(upgraded)
    assert upgraded.RECEIPT_PROJECTOR_REVISION == current.RECEIPT_PROJECTOR_REVISION
    assert upgraded.ReceiptSnapshotStore(tmp_path).read("receipt", "a") is None
    assert upgraded.ReceiptSnapshotStore(tmp_path).generation() is None
    assert store.generation() == generation  # incompatible read never rewrites


def test_clear_cannot_remove_a_newer_generation(tmp_path):
    store = ReceiptSnapshotStore(tmp_path)
    first = publish(store)
    second = publish(store, "two")
    assert store.clear(expected_generation_id=first.generation_id) is False
    assert store.generation() == second
    assert store.clear(expected_generation_id=second.generation_id) is True
    assert store.generation() is None and store.read("receipt", "a") is None


def test_concurrent_read_payload_and_metadata_always_share_generation(tmp_path):
    store = ReceiptSnapshotStore(tmp_path)
    publish(store)
    errors = []

    def writer():
        try:
            for n in range(20):
                publish(store, str(n))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    while thread.is_alive():
        entry = store.read("receipt", "a")
        if entry is not None:
            assert entry.payload["token"] == entry.input_token
    thread.join()
    assert not errors


def test_corrupt_or_foreign_sqlite_fails_closed_without_replacing_file(tmp_path):
    store = ReceiptSnapshotStore(tmp_path)
    store.root.mkdir()
    store.path.write_bytes(b"not sqlite")
    original = store.path.read_bytes()
    assert store.generation() is None and store.read("r", "a") is None
    assert store.path.read_bytes() == original
