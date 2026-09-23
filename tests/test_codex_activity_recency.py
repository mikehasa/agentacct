"""Codex settings/metadata writes must not make an idle conversation recent."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentacct.api import create_local_api_app
from agentacct.cli import app
from agentacct.client_usage import discover_codex_usage, plan_local_usage_import
from agentacct.receipt_snapshot_runtime import ReceiptSnapshotManager
from agentacct.service import SentinelService
from tests.test_client_usage import _codex_counters, _codex_token_count, _make_codex_home
from tests.test_receipt_snapshot_api import HEADERS, publish
from tests.test_receipt_api import TOKEN, _auth

START = 1789644853  # 2026-09-17 11:34:13 UTC
WORK = 1789645495   # last assistant response / completed turn
SETTINGS = 1790155960  # settings changed six days later


def _stamp(value: int) -> str:
    return datetime.fromtimestamp(value).astimezone().isoformat()


def _record(kind: str, at: int, payload: dict) -> dict:
    return {"type": kind, "timestamp": _stamp(at), "payload": payload}


def _home(tmp_path: Path, *, with_usage: bool = True) -> tuple[Path, Path]:
    home = _make_codex_home(tmp_path)
    rollout = next((home / "sessions").rglob("rollout-*.jsonl"))
    records = [_record("session_meta", START, {"id": "session-abc"})]
    if with_usage:
        records.append(_codex_token_count(_stamp(WORK - 1), _codex_counters(1000, 100, 50, 0)))
    records.extend([
        _record("response_item", WORK, {"type": "message", "role": "assistant"}),
        _record("event_msg", WORK, {"type": "task_complete"}),
        _record("event_msg", SETTINGS, {"type": "thread_settings_applied"}),
    ])
    rollout.write_text("".join(json.dumps(row) + "\n" for row in records))
    os.utime(rollout, (SETTINGS, SETTINGS))
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("alter table threads add column recency_at integer")
        db.execute("update threads set created_at=?, updated_at=?, recency_at=?, tokens_used=?",
                   (START, SETTINGS, WORK - 100, 1050 if with_usage else 0))
    return home, rollout


@pytest.mark.parametrize("with_usage", [True, False])
def test_activity_comes_from_last_work_record_not_settings_or_file_mtime(tmp_path, with_usage):
    home, rollout = _home(tmp_path, with_usage=with_usage)
    observations = []
    usage = discover_codex_usage(codex_home=home, _session_observations=observations)
    assert len(observations) == 1
    assert observations[0].updated_at == WORK
    assert observations[0].source_revision_at == rollout.stat().st_mtime_ns
    assert observations[0].started_at == START
    assert [row.updated_at for row in usage] == ([WORK] if with_usage else [])


def test_rollout_only_session_uses_work_time_not_metadata_file_revision(tmp_path):
    home, _ = _home(tmp_path)
    (home / "state_5.sqlite").unlink()
    observations = []
    usage = discover_codex_usage(codex_home=home, _session_observations=observations)
    assert usage[0].updated_at == observations[0].updated_at == WORK


def test_settings_only_change_does_not_refresh_usage_but_new_work_does(tmp_path):
    home, rollout = _home(tmp_path)
    original = discover_codex_usage(codex_home=home)[0]
    stored = original.to_sentinel_event()
    # Simulate another settings write without changing usage/work timestamps.
    with rollout.open("a") as stream:
        stream.write(json.dumps(_record("event_msg", SETTINGS + 100, {"type": "thread_settings_applied"})) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("update threads set updated_at=?", (SETTINGS + 100,))
    candidate = discover_codex_usage(codex_home=home)[0]
    plan = plan_local_usage_import([candidate], [stored])
    assert plan.unchanged_candidates == [candidate]
    assert not plan.refresh_candidates
    with rollout.open("a") as stream:
        stream.write(json.dumps(_record("event_msg", SETTINGS + 200, {"type": "task_started"})) + "\n")
    active = discover_codex_usage(codex_home=home)[0]
    assert active.updated_at == SETTINGS + 200
    assert plan_local_usage_import([active], [stored]).refresh_candidates == [active]


@pytest.mark.parametrize("rollout_available", [True, False])
def test_dedicated_db_recency_is_fallback_when_work_timestamps_are_unavailable(tmp_path, rollout_available):
    home, rollout = _home(tmp_path)
    if rollout_available:
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "session-abc"}}) + "\n")
    else:
        rollout.unlink()
    observations = []
    usage = discover_codex_usage(codex_home=home, _session_observations=observations)
    assert usage[0].updated_at == observations[0].updated_at == WORK - 100


def test_legacy_db_without_recency_keeps_fallback_for_untimestamped_rollout(tmp_path):
    home = _make_codex_home(tmp_path)
    assert discover_codex_usage(codex_home=home)[0].updated_at == 200


def test_metadata_only_thread_does_not_inherit_settings_timestamp(tmp_path):
    home, rollout = _home(tmp_path, with_usage=False)
    rollout.write_text("\n".join([
        json.dumps(_record("session_meta", START, {"id": "session-abc"})),
        json.dumps(_record("event_msg", SETTINGS, {"type": "thread_settings_applied"})),
        json.dumps(_record("future_metadata", SETTINGS + 1, {})),
    ]) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("update threads set recency_at=null")
    observations = []
    assert not discover_codex_usage(codex_home=home, _session_observations=observations)
    assert observations[0].updated_at == START


@pytest.mark.parametrize("snapshot", [False, True])
def test_refresh_repairs_old_recency_across_sessions_tasks_and_receipts(tmp_path, monkeypatch, snapshot):
    monkeypatch.setattr(ReceiptSnapshotManager, "start", lambda self: None)
    home, _ = _home(tmp_path)
    store = tmp_path / "store"
    service = SentinelService(store)
    corrected = discover_codex_usage(codex_home=home)[0]
    # Simulate the previously imported row: correct tokens, wrong activity date.
    old = replace(corrected, updated_at=SETTINGS)
    service.record_event(old.to_sentinel_event(), trusted_usage_import=True)
    newer = replace(corrected, client_session_id="recent-work", title="Recent work",
                    started_at=WORK + 100, updated_at=WORK + 200)
    service.record_event(newer.to_sentinel_event(), trusted_usage_import=True)
    result = CliRunner().invoke(app, ["usage", "import-local", "--store-dir", str(store),
        "--client", "codex", "--codex-home", str(home), "--refresh", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["refreshed_events"] == 1
    if snapshot:
        publish(store)
    headers = HEADERS if snapshot else _auth()
    with TestClient(create_local_api_app(store_dir=store, v1_auth_token=TOKEN)) as http:
        sessions = http.get("/v1/sessions", headers=headers).json()["sessions"]
        assert [row["client_session_id"] for row in sessions] == ["recent-work", "session-abc"]
        old_session = sessions[1]
        assert old_session["last_activity_at"] == WORK
        assert old_session["duration_seconds"] == WORK - START
        tasks = http.get("/v1/tasks", headers=headers).json()["tasks"]
        assert [row["last_activity_at"] for row in tasks] == [WORK + 200, WORK]
        receipt = http.get("/v1/receipt", headers=headers, params={"task": tasks[1]["task_id"]}).json()
        assert receipt["duration_seconds"] == WORK - START
        assert receipt["sessions"][0]["members"][0]["last_activity_at"] == WORK


def test_database_recency_keeps_new_turn_visible_before_rollout_flush(tmp_path):
    home, _ = _home(tmp_path)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("update threads set recency_at=?", (WORK + 100,))
    assert discover_codex_usage(codex_home=home)[0].updated_at == WORK + 100


def test_replayed_parent_work_does_not_end_before_child_creation(tmp_path):
    home, _ = _home(tmp_path)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("update threads set created_at=?, recency_at=null", (WORK + 100,))
    assert discover_codex_usage(codex_home=home)[0].updated_at == WORK + 100
