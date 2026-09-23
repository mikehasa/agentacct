"""Independent capture, invalidation and session-identity edge regressions."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

import agentacct.api as api
from agentacct.receipt_snapshot_builder import snapshot_input_state
from agentacct.receipt_snapshot_runtime import ReceiptSnapshotManager
from agentacct.receipt_snapshot_store import ReceiptSnapshotStore
from agentacct.service import SentinelService
from tests.test_receipt_snapshot_api import HEADERS, client, publish, seed
from tests.test_receipt_snapshot_invalidation import _evidence
from tests.test_receipt_api import _auth
from tests.test_v1_sessions import _record_usage


@pytest.fixture(autouse=True)
def no_automatic_worker(monkeypatch):
    monkeypatch.setattr(ReceiptSnapshotManager, "start", lambda self: None)


def _backup(source: Path, destination: Path) -> None:
    reader, writer = sqlite3.connect(source), sqlite3.connect(destination)
    try:
        reader.backup(writer)
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("mutation", ["events", "evidence", "continuation"])
def test_destructive_change_after_capture_does_not_publish_old_private_payload(
    tmp_path, monkeypatch, mutation,
):
    service = seed(tmp_path)
    evidence = service.evidence.store
    evidence.append(_evidence("private-hook"))
    old = publish(tmp_path)
    original = api._dashboard_page_data

    def mutate_after_capture(*args, **kwargs):
        if mutation == "events":
            service.replace_events(lambda event: True, [])
        elif mutation == "evidence":
            with sqlite3.connect(evidence.projection_path) as connection:
                connection.execute("DELETE FROM evidence_versions")
        else:
            path = tmp_path / "continuation-tasks" / "actions.jsonl"
            path.parent.mkdir(exist_ok=True)
            path.write_text("")
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "_dashboard_page_data", mutate_after_capture)
    with pytest.raises(RuntimeError, match="changed destructively"):
        publish(tmp_path)
    assert ReceiptSnapshotStore(tmp_path).generation().generation_id == old.generation_id
    response = client(tmp_path).get("/v1/tasks", headers=HEADERS)
    assert response.status_code == 202
    assert "tasks" not in response.json()


@pytest.mark.parametrize("relative", [
    "task-identity/secret.key", "cost_events.jsonl", "continuation-tasks/actions.jsonl",
    "runs/example/metadata.json", "runs/example/outcome.json", "runs/example/stdout.log",
])
def test_same_content_atomic_file_replacement_invalidates_snapshot(tmp_path, relative):
    service = seed(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"x" * 32 if path.name == "secret.key" else b"{}" if path.suffix == ".json" else b"")
    publish(tmp_path)
    before = snapshot_input_state(tmp_path, service)
    original = path.stat()
    replacement = path.with_name(path.name + ".replacement")
    replacement.write_bytes(path.read_bytes())
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, path)
    assert snapshot_input_state(tmp_path, service)[1] != before[1]
    assert client(tmp_path).get("/v1/tasks", headers=HEADERS).status_code == 202


def test_new_evidence_store_invalidates_generation_built_without_it(tmp_path):
    # Start without any events or EvidenceRuntime.store access.
    empty_root = tmp_path / "empty"
    empty_service = SentinelService(empty_root)
    publish(empty_root)
    before = snapshot_input_state(empty_root, empty_service)
    empty_service.evidence.store.append(_evidence("new-hook"))
    assert snapshot_input_state(empty_root, empty_service)[1] != before[1]
    response = client(empty_root).get("/v1/tasks", headers=HEADERS)
    assert response.status_code == 202
    assert "tasks" not in response.json()


@pytest.mark.parametrize("kind", ["events", "evidence"])
def test_in_place_sqlite_backup_restore_refuses_payload_removed_by_restore(tmp_path, kind):
    service = seed(tmp_path)
    evidence = service.evidence.store
    evidence.append(_evidence("retained-hook"))
    path = service.event_log.db_path if kind == "events" else evidence.projection_path
    backup = tmp_path / "earlier.sqlite3"
    _backup(path, backup)
    if kind == "events":
        _record_usage(service, session_id="removed-by-restore", tokens=10, updated_at=time.time())
    else:
        evidence.append(_evidence("removed-by-restore"))
    publish(tmp_path)
    before = snapshot_input_state(tmp_path, service)
    inode = path.stat().st_ino
    _backup(backup, path)
    assert path.stat().st_ino == inode
    assert snapshot_input_state(tmp_path, service)[1] != before[1]
    response = client(tmp_path).get("/v1/tasks", headers=HEADERS)
    assert response.status_code == 202
    assert "tasks" not in response.json()


def test_snapshot_session_capture_preserves_children_orphans_and_namespace_refusals(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    for session, parent, namespace in (
        ("root", None, "sha256:home"),
        ("child", "root", "sha256:home"),
        ("foreign", "root", "sha256:other-home"),
        ("orphan", "missing", "sha256:home"),
    ):
        _record_usage(service, session_id=session, tokens=100,
                      updated_at=now - 60, session_kind="child" if parent else None,
                      parent_session_id=parent, namespace=namespace)
    http = client(tmp_path)
    paths = ["/v1/tasks", "/v1/sessions?roots_only=true", "/v1/sessions?roots_only=false"]
    paths += [f"/v1/session?client=claude-code&session_id={session}" for session in ("root", "child", "foreign", "orphan")]
    expected = {path: http.get(path, headers=_auth()).json() for path in paths}
    publish(tmp_path)
    for path in paths:
        response = http.get(path, headers=HEADERS)
        assert response.status_code == 200, (path, response.text)
        actual = response.json()
        actual.pop("projection")
        actual.pop("generated_at", None)
        expected[path].pop("generated_at", None)
        assert actual == expected[path], path
    roots = expected["/v1/sessions?roots_only=true"]["sessions"]
    assert {row["client_session_id"] for row in roots} == {"root", "foreign", "orphan"}
