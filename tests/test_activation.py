from __future__ import annotations

import stat
import time

from fastapi.testclient import TestClient

from agent_chronicle.activation import ActivationStateStore, build_activation_snapshot
from agent_chronicle.api import create_local_api_app
from agent_chronicle.ingestion_health import IngestionHealthStore
from agent_chronicle.service import SentinelService


def _runtime(state: str = "running") -> dict[str, object]:
    return {"state": state, "dashboard_url": "http://127.0.0.1:8765/"}


def test_activation_never_calls_an_empty_store_caught_up() -> None:
    payload = build_activation_snapshot(
        source_rows=[],
        ingestion_health={"watcher": {"state": "not_configured"}},
        task_projection={"tasks": []},
        runtime_status=_runtime("stopped"),
        recording_configured=False,
    )

    assert payload["stage"] == "client_needed"
    assert payload["headline"] == "Connect a coding-agent source"
    assert payload["ready"] is False
    assert payload["primary_action"]["command"] == "agentacct usage discover-sources"


def test_activation_requires_a_new_session_after_configuration() -> None:
    payload = build_activation_snapshot(
        source_rows=[{"client": "codex", "status": "found"}],
        ingestion_health={"watcher": {"state": "running"}},
        task_projection={"tasks": []},
        runtime_status=_runtime(),
        recording_configured=True,
        new_session_required=True,
    )

    assert payload["stage"] == "new_session_needed"
    assert payload["primary_action"]["label"] == "Open a new agent chat"
    assert payload["progress"][2]["state"] == "restart_required"


def test_waiting_activation_copy_does_not_imply_whole_client_support() -> None:
    payload = build_activation_snapshot(
        source_rows=[{"client": "codex", "status": "found"}],
        ingestion_health={"watcher": {"state": "running"}},
        task_projection={"tasks": []},
        runtime_status=_runtime(),
        recording_configured=True,
    )

    assert payload["stage"] == "waiting_for_task"
    assert "recognized session" in payload["primary_action"]["reason"]
    assert "supported session" not in payload["primary_action"]["reason"]


def test_usage_only_real_task_is_honest_degraded_success() -> None:
    payload = build_activation_snapshot(
        source_rows=[{"client": "codex", "status": "found"}],
        ingestion_health={"watcher": {"state": "running"}},
        task_projection={"tasks": [{"task_id": "opaque", "work_items": []}]},
        runtime_status=_runtime(),
        recording_configured=True,
    )

    assert payload["stage"] == "active"
    assert payload["ready"] is True
    assert payload["progress"][-1]["state"] == "captured"
    assert payload["progress"][-1]["detail"] == "usage/activity only"


def test_semantic_task_reports_semantic_context_without_requiring_a_check() -> None:
    payload = build_activation_snapshot(
        source_rows=[{"client": "claude-code", "status": "found"}],
        ingestion_health={"watcher": {"state": "running"}},
        task_projection={"tasks": [{"task_id": "opaque", "work_items": [{"status": "started"}]}]},
        runtime_status=_runtime(),
        recording_configured=True,
    )

    assert payload["ready"] is True
    assert payload["progress"][-1]["detail"] == "semantic context recorded"


def test_activation_install_boundary_is_owner_only_and_read_only_when_missing(tmp_path) -> None:
    store = ActivationStateStore(tmp_path / "state")

    assert store.snapshot() is None
    assert not store.root.exists()

    written = store.mark_configured(
        project_dir=tmp_path,
        clients=["codex", "codex", "claude-code"],
        configured_at=123.5,
    )

    assert written["clients"] == ["claude-code", "codex"]
    assert store.snapshot() == written
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.state_path.stat().st_mode) == 0o600

    repeated = store.mark_configured(
        project_dir=tmp_path,
        clients=["codex"],
        configured_at=999.0,
    )
    assert repeated["configured_at"] == 123.5
    assert repeated["clients"] == ["claude-code", "codex"]


def _usage(service: SentinelService, *, session: str, started_at: float) -> None:
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-test",
            "estimated_input_tokens": 10,
            "estimated_output_tokens": 2,
            "usage_confidence": "client_reported",
            "cost_confidence": "unknown",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session,
                "client_session_kind": "root",
                "cache_creation_tokens_reported": False,
                "cache_read_tokens_reported": True,
                "started_at": started_at,
                "updated_at": started_at,
            },
        },
        trusted_usage_import=True,
    )


def test_empty_home_leads_with_one_actionable_activation_card(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    payload = client.get("/api/activation").json()
    page = client.get("/").text

    assert payload["stage"] == "client_needed"
    assert payload["primary_action"]["command"] == "agentacct usage discover-sources"
    assert 'class="activation-card"' in page
    assert "Get to your first real Task" in page
    assert page.count('class="activation-action"') == 1
    assert "It does not request provider API keys." in page


def test_only_a_post_configuration_chat_completes_activation(tmp_path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _usage(service, session="existing-chat", started_at=100.0)
    ActivationStateStore(store_root).mark_configured(
        project_dir=tmp_path,
        clients=["codex"],
        configured_at=200.0,
    )
    health = IngestionHealthStore(store_root)
    health.acquire_watcher(
        lease_id="activation-watch",
        pid=4242,
        importer_version="integration-test",
        interval_seconds=60,
        scan_limit=20,
        sources=("codex",),
        now=time.time(),
    )
    client = TestClient(create_local_api_app(store_dir=store_root))

    before = client.get("/api/activation").json()
    assert before["stage"] == "new_session_needed"
    assert before["task_count"] == 0

    _usage(service, session="new-chat", started_at=300.0)
    after = client.get("/api/activation").json()
    page = client.get("/").text

    assert after["stage"] == "active"
    assert after["ready"] is True
    assert after["task_count"] == 1
    assert 'class="activation-card"' not in page
