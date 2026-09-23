"""End-to-end snapshot lane: stored output parity, freshness and privacy."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import agentacct.api as api
from agentacct.receipt_snapshot_builder import build_snapshot_entries, snapshot_input_state
from agentacct.receipt_snapshot_runtime import ReceiptSnapshotManager
from agentacct.receipt_snapshot_store import ReceiptSnapshotStore
from agentacct.service import SentinelService
from tests.test_receipt_api import TOKEN, _auth, _record_usage, _record_section, _record_failed_check

HEADERS = {**_auth(), "X-Agentacct-Read-Mode": "snapshot"}
REAL_MANAGER_START = ReceiptSnapshotManager.start


@pytest.fixture(autouse=True)
def no_automatic_worker(monkeypatch):
    monkeypatch.setattr(ReceiptSnapshotManager, "start", lambda self: None)


def seed(path):
    service = SentinelService(path)
    _record_usage(service, session_id="one", at=time.time() - 60)
    _record_section(service, session_id="one", section_id="step", status="completed", at=time.time() - 50)
    _record_failed_check(service, session_id="one", section_id="step", at=time.time() - 45)
    return service


def publish(path):
    entries, token, safety, captured = build_snapshot_entries(path)
    return ReceiptSnapshotStore(path).publish(entries, input_token=token, safety_token=safety, built_at=captured)


def client(path):
    return TestClient(api.create_local_api_app(store_dir=path, v1_auth_token=TOKEN))


def test_first_read_is_pending_without_building_or_waking_legacy_warmer(tmp_path, monkeypatch):
    seed(tmp_path)
    app = api.create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN)
    monkeypatch.setattr(api, "build_work_ledger", lambda *a, **k: pytest.fail("foreground rebuilt ledger"))
    with TestClient(app) as http:
        response = http.get("/v1/tasks", headers=HEADERS)
        assert response.status_code == 202
        assert response.json()["projection"]["available"] is False
        assert app.state.ledger_warmer._thread is None
        assert http.get("/v1/tasks", headers={"X-Agentacct-Read-Mode": "snapshot"}).status_code == 401


def test_all_snapshot_views_match_existing_semantics_and_survive_restart(tmp_path, monkeypatch):
    seed(tmp_path)
    http = client(tmp_path)
    expected_tasks = http.get("/v1/tasks", headers=_auth()).json()
    task = expected_tasks["tasks"][0]["task_id"]
    paths = ["/v1/tasks", "/v1/attention", f"/v1/receipt?task={task}",
             "/v1/sessions?roots_only=false", "/v1/session?client=claude-code&session_id=one"]
    expected = {path: http.get(path, headers=_auth()).json() for path in paths}
    generation = publish(tmp_path)
    http = client(tmp_path)
    monkeypatch.setattr(api, "build_work_ledger", lambda *a, **k: pytest.fail("rebuilt after restart"))
    for path in paths:
        response = http.get(path, headers=HEADERS)
        assert response.status_code == 200, response.text
        actual = response.json()
        metadata = actual.pop("projection")
        assert metadata["generation"] == generation.generation_id
        assert metadata["built_at"] == generation.built_at
        assert metadata["state"] == "current"
        # Timestamps and the opaque attention fingerprint describe build
        # instances, not receipt semantics.
        for field in ("generated_at", "snapshot"):
            actual.pop(field, None)
            expected[path].pop(field, None)
        def stable(value):
            if isinstance(value, dict):
                # HTML form nonces belong to one server lifetime and are not
                # used by the bearer-authenticated native disposition route.
                return {key: stable(item) for key, item in value.items() if key != "action_token"}
            if isinstance(value, list):
                return [stable(item) for item in value]
            return value
        assert stable(actual) == stable(expected[path])
    assert http.get("/v1/receipt?task=missing", headers=HEADERS).status_code == 404


def test_append_during_expensive_build_publishes_then_stays_updating(tmp_path, monkeypatch):
    service = seed(tmp_path)
    old = publish(tmp_path)
    entered, release = threading.Event(), threading.Event()
    real = api.build_work_ledger

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "build_work_ledger", blocked)
    with ThreadPoolExecutor() as executor:
        building = executor.submit(publish, tmp_path)
        assert entered.wait(5)
        _record_usage(service, session_id="two", at=time.time())
        start = time.monotonic()
        response = client(tmp_path).get("/v1/tasks", headers=HEADERS)
        assert time.monotonic() - start < 1.0
        assert response.status_code == 200
        assert response.json()["projection"]["generation"] == old.generation_id
        assert response.json()["projection"]["state"] == "updating"
        release.set()
        generation = building.result(timeout=5)
    assert generation.input_token != snapshot_input_state(tmp_path, service)[0]
    assert client(tmp_path).get("/v1/tasks", headers=HEADERS).json()["projection"]["state"] == "updating"


def test_deletion_refuses_previous_payload_and_build_races(tmp_path, monkeypatch):
    service = seed(tmp_path)
    publish(tmp_path)
    http = client(tmp_path)
    service.replace_events(lambda event: True, [])
    response = http.get("/v1/tasks", headers=HEADERS)
    assert response.status_code == 202
    assert response.json()["projection"]["available"] is False
    assert "tasks" not in response.json()
    assert publish(tmp_path)
    assert http.get("/v1/tasks", headers=HEADERS).json()["tasks"] == []


def test_secondary_file_rewrite_invalidates_even_with_same_size_and_no_event(tmp_path):
    service = seed(tmp_path)
    publish(tmp_path)
    before = snapshot_input_state(tmp_path, service)
    (tmp_path / "cost_events.jsonl").write_text("{}\n")
    after = snapshot_input_state(tmp_path, service)
    assert before[0] != after[0] and before[1] != after[1]
    assert client(tmp_path).get("/v1/tasks", headers=HEADERS).status_code == 202


def test_timeline_cursor_never_mixes_generations_or_tasks(tmp_path):
    service = seed(tmp_path)
    publish(tmp_path)
    http = client(tmp_path)
    task = http.get("/v1/tasks", headers=HEADERS).json()["tasks"][0]["task_id"]
    path = f"/v1/task-timeline?task={task}&limit=1"
    first = http.get(path, headers=HEADERS).json()
    cursor = first["next_cursor"]
    assert cursor
    second = http.get(path + "&cursor=" + cursor, headers=HEADERS).json()
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["projection"]["generation"] == first["projection"]["generation"]
    _record_usage(service, session_id="two", at=time.time())
    publish(tmp_path)
    assert http.get(path + "&cursor=" + cursor, headers=HEADERS).status_code == 409


def test_worker_process_publishes_complete_generation(tmp_path):
    seed(tmp_path)
    import os
    env = {**os.environ, "PYTHONPATH": str(api.Path(api.__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-m", "agentacct.receipt_snapshot_worker", "--store-dir", str(tmp_path)],
                            env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    generation = ReceiptSnapshotStore(tmp_path).generation()
    assert generation is not None and generation.cpu_seconds > 0
    assert generation.published_at >= generation.built_at
    assert client(tmp_path).get("/v1/tasks", headers=HEADERS).status_code == 200


def test_legacy_backup_state_revokes_old_payload_but_worker_can_reinitialize(tmp_path):
    import sqlite3
    service = seed(tmp_path)
    publish(tmp_path)
    http = client(tmp_path)
    with sqlite3.connect(service.event_log.db_path) as connection:
        connection.execute("DROP TABLE event_log_snapshot_state")
    # Input metadata is still sufficient to schedule repair; no old payload is
    # treated as safe merely because its original guard table disappeared.
    assert snapshot_input_state(tmp_path, service)
    assert http.get("/v1/tasks", headers=HEADERS).status_code == 202
    publish(tmp_path)
    assert http.get("/v1/tasks", headers=HEADERS).status_code == 200


def test_native_read_activates_real_background_worker_and_reuses_its_generation(tmp_path, monkeypatch):
    seed(tmp_path)
    monkeypatch.setattr(ReceiptSnapshotManager, "start", REAL_MANAGER_START)
    app = api.create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN)
    with TestClient(app) as http:
        assert http.get("/v1/tasks", headers=HEADERS).status_code == 202
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = http.get("/v1/tasks", headers=HEADERS)
            if response.status_code == 200:
                break
            time.sleep(0.05)
        assert response.status_code == 200, response.text
        generation = response.json()["projection"]["generation"]
        for _ in range(5):
            assert http.get("/v1/tasks", headers=HEADERS).json()["projection"]["generation"] == generation
        assert app.state.ledger_warmer._thread is None
    assert app.state.receipt_snapshots._closed
