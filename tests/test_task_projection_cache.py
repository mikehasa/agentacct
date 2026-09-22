"""The /v1 task projection cache (the Tasks list and every receipt).

Contract under test: the projection is keyed on everything it is built from,
so no store change can be hidden from the next reader — not an event, not a
secondary-store append, and not a continuation edit, which mints no ledger
event at all. The 30 s bound that remains covers only the wall clock. The
background warmer builds the projection too, without counting as a reader.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import agentacct.api as api_module
from agentacct.api import create_local_api_app
from agentacct.service import SentinelService
from agentacct.task_continuations import ClientSessionRef, ContinuationTaskStore

TOKEN = "test-v1-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _record_usage(service: SentinelService, *, session: str, started_at: float) -> None:
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-cache-test",
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 20,
            "estimated_cost_usd": 0.01,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session,
                "client_session_kind": "root",
                "cached_input_tokens": 0,
                "cache_creation_tokens_reported": False,
                "cache_read_tokens_reported": True,
                "started_at": started_at,
                "updated_at": started_at,
            },
        },
        trusted_usage_import=True,
    )


def _session_ref(session: str) -> ClientSessionRef:
    return ClientSessionRef(client="codex", client_session_id=session)


def _two_root_sessions(store_root: Path) -> None:
    service = SentinelService(store_root)
    _record_usage(service, session="first-root-chat", started_at=1_750_000_000.0)
    _record_usage(service, session="continued-root-chat", started_at=1_750_000_100.0)


def _counting_projection_builds(monkeypatch) -> dict[str, int]:
    calls = {"count": 0}
    real_build = api_module._dashboard_task_projection

    def _counting_build(*args: Any, **kwargs: Any):
        calls["count"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(api_module, "_dashboard_task_projection", _counting_build)
    return calls


def _task_total(client: TestClient) -> int:
    response = client.get("/v1/tasks", headers=AUTH)
    assert response.status_code == 200
    return int(response.json()["total"])


def _append_cost_event(store_root: Path) -> None:
    with (store_root / "cost_events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event_id": "cost_test",
                    "created_at": time.time(),
                    "run_id": None,
                    "decision": "record",
                    "reason": "",
                    "estimated_cost_usd": 0.0,
                    "estimated_input_tokens": 0,
                    "estimated_output_tokens": 0,
                },
                sort_keys=True,
            )
            + "\n"
        )


def test_a_continuation_link_is_visible_to_the_very_next_reader(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    _two_root_sessions(store_root)
    client = TestClient(create_local_api_app(store_dir=store_root, v1_auth_token=TOKEN))
    assert _task_total(client) == 2

    # Grouping two chats into one Task records no ledger event; only the
    # continuation store moves.
    result = ContinuationTaskStore(store_root).link_sessions(
        _session_ref("first-root-chat"),
        _session_ref("continued-root-chat"),
        confirmed_by="dashboard-user",
    )
    assert result.changed is True

    assert _task_total(client) == 1  # not "within 30 seconds": now

    ContinuationTaskStore(store_root).unlink_session(
        result.task_id, _session_ref("continued-root-chat"), confirmed_by="dashboard-user"
    )
    assert _task_total(client) == 2


def test_a_secondary_store_change_rebuilds_the_projection_at_once(tmp_path: Path, monkeypatch) -> None:
    store_root = tmp_path / "state"
    _two_root_sessions(store_root)
    calls = _counting_projection_builds(monkeypatch)
    client = TestClient(create_local_api_app(store_dir=store_root, v1_auth_token=TOKEN))

    assert _task_total(client) == 2
    assert calls["count"] == 1

    # A cost event moves no primary ledger event, so the events fingerprint is
    # unchanged; the shared ledger rebuilds for it, and so must what is
    # assembled over that ledger.
    _append_cost_event(store_root)
    assert _task_total(client) == 2
    assert calls["count"] == 2


def test_an_unchanged_store_reuses_the_projection_across_routes(tmp_path: Path, monkeypatch) -> None:
    store_root = tmp_path / "state"
    _two_root_sessions(store_root)
    calls = _counting_projection_builds(monkeypatch)
    client = TestClient(create_local_api_app(store_dir=store_root, v1_auth_token=TOKEN))

    tasks = client.get("/v1/tasks", headers=AUTH).json()["tasks"]
    assert calls["count"] == 1
    for _ in range(3):
        assert _task_total(client) == 2
    receipt = client.get("/v1/receipt", headers=AUTH, params={"task": tasks[0]["task_id"]})
    assert receipt.status_code == 200
    assert client.get("/v1/attention", headers=AUTH).status_code == 200
    assert calls["count"] == 1  # one build served the list, a receipt and attention


def test_the_wall_clock_bound_still_recomputes_on_an_unchanged_store(tmp_path: Path, monkeypatch) -> None:
    store_root = tmp_path / "state"
    _two_root_sessions(store_root)
    calls = _counting_projection_builds(monkeypatch)
    clock = [api_module.time.time()]
    monkeypatch.setattr(api_module.time, "time", lambda: clock[0])
    client = TestClient(create_local_api_app(store_dir=store_root, v1_auth_token=TOKEN))

    assert _task_total(client) == 2
    clock[0] += 29.0
    assert _task_total(client) == 2
    assert calls["count"] == 1

    # The weekly-plan shares are calibrated over a window ending "now", so an
    # unchanged store is still recomputed as time passes.
    clock[0] += 2.0
    assert _task_total(client) == 2
    assert calls["count"] == 2


def test_the_warmer_builds_the_projection_and_is_not_a_reader(tmp_path: Path, monkeypatch) -> None:
    store_root = tmp_path / "state"
    _two_root_sessions(store_root)
    calls = _counting_projection_builds(monkeypatch)
    app = create_local_api_app(store_dir=store_root, v1_auth_token=TOKEN)
    warmer = app.state.ledger_warmer

    warmer.rebuild_now()
    assert calls["count"] == 1
    assert warmer.is_watching() is False  # its own build must not keep it awake

    client = TestClient(app)
    assert _task_total(client) == 2  # served from the warm build
    assert calls["count"] == 1
    assert warmer.is_watching() is True


def test_a_continuation_change_is_a_change_the_warmer_rebuilds(tmp_path: Path, monkeypatch) -> None:
    store_root = tmp_path / "state"
    _two_root_sessions(store_root)
    calls = _counting_projection_builds(monkeypatch)
    app = create_local_api_app(store_dir=store_root, v1_auth_token=TOKEN)
    warmer = app.state.ledger_warmer
    client = TestClient(app)

    assert _task_total(client) == 2  # a reader; this request builds
    assert calls["count"] == 1
    warmer.rebuild_now()  # built for the store as it is; nothing new to build
    assert calls["count"] == 1

    ContinuationTaskStore(store_root).link_sessions(
        _session_ref("first-root-chat"),
        _session_ref("continued-root-chat"),
        confirmed_by="dashboard-user",
    )
    assert warmer.check_once() is False  # noticed; waiting for the store to settle
    time.sleep(0.45)
    assert warmer.check_once() is True
    assert calls["count"] == 2

    assert _task_total(client) == 1  # the reader pays no rebuild and sees the link
    assert calls["count"] == 2
