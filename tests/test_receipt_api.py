"""The /v1/receipt and /v1/tasks native-shell lane."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.receipt import RECEIPT_SCHEMA_VERSION
from agentacct.service import SentinelService

TOKEN = "test-v1-token"
NS = "sha256:receipt-api-ns"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _app(tmp_path: Path) -> TestClient:
    return TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))


def _record_usage(service: SentinelService, *, session_id: str, at: float) -> None:
    service.record_event(
        {
            "event_id": f"evt_usage_{session_id}",
            "created_at": at,
            "source": "claude-code-local-session-import",
            "event_type": "model_usage",
            "run_id": None,
            "provider": "claude-code",
            "model": "claude-opus-4-8",
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 25,
            "estimated_cost_usd": 0.5,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "cost_basis": "pricing_table",
            "metadata": {
                "usage_source": "local_client_session_store",
                "usage_provenance": "agent_sentinel_local_usage_import",
                "client": "claude-code",
                "client_session_id": session_id,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "project_dir": "/tmp/project",
                "started_at": at,
                "updated_at": at,
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "source_namespace_fingerprint": NS,
            },
        },
        trusted_usage_import=True,
    )


def _record_section(service: SentinelService, *, session_id: str, section_id: str, status: str, at: float) -> None:
    service.record_event(
        {
            "event_id": f"evt_section_{session_id}_{section_id}_{status}",
            "created_at": at,
            "source": "claude-code",
            "event_type": f"section_{status}",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": "claude-code",
                "client_session_id": session_id,
                "client_context_keys_authored": ["client_session_id"],
                "project_dir": "/tmp/project",
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "section_id": section_id,
                "section_status": status,
                "section_title": "Add rate limit to login",
                "kind": "implementation",
                "files": ["src/login.py"],
            },
        }
    )


def _record_passing_check(service: SentinelService, *, session_id: str, section_id: str, at: float) -> None:
    service.record_event(
        {
            "event_id": f"evt_check_{session_id}_{section_id}",
            "created_at": at,
            "source": "claude-code",
            "event_type": "machine_check",
            "run_id": None,
            "metadata": {
                "result": "passed",
                "evidence_type": "test",
                "summary": "pytest passed",
                "name": "pytest",
                "exit_code": 0,
                "section_id": section_id,
                "client": "claude-code",
                "client_session_id": session_id,
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "project_dir": "/tmp/project",
            },
        }
    )


def test_receipt_routes_require_a_bearer_token(tmp_path: Path) -> None:
    client = _app(tmp_path)
    assert client.get("/v1/tasks").status_code == 401
    assert client.get("/v1/receipt?task=task_x").status_code == 401


def test_version_advertises_the_receipt_schema(tmp_path: Path) -> None:
    version = _app(tmp_path).get("/v1/version", headers=_auth()).json()
    assert version["receipt_schema"] == RECEIPT_SCHEMA_VERSION


def test_tasks_list_and_receipt_detail_for_an_observed_task(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    client = _app(tmp_path)

    listing = client.get("/v1/tasks", headers=_auth()).json()
    assert listing["schema"] == RECEIPT_SCHEMA_VERSION
    assert listing["total"] == 1
    row = listing["tasks"][0]
    task_id = row["task_id"]
    assert task_id.startswith("task_")
    assert "decision_status" in row and "evidence_strength" in row

    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert set(receipt["dimensions"]) == {
        "task",
        "actors",
        "actions",
        "cost",
        "evidence",
        "outcome",
        "gaps",
        "provenance",
    }
    assert receipt["axes"]["decision_status"]["asserted_by"] in {"agent_report", "human", "machine", "none"}
    # cost_basis threads all the way to the wire.
    assert receipt["dimensions"]["cost"]["cost_basis"] == "pricing_table"


def test_unknown_task_is_a_404_not_an_empty_fabrication(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    client = _app(tmp_path)
    assert client.get("/v1/receipt?task=task_deadbeef", headers=_auth()).status_code == 404


def test_receipt_reports_verified_when_a_current_check_passes(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-1", status="completed", at=100.0)
    _record_passing_check(service, session_id="s1", section_id="sec-1", at=200.0)
    client = _app(tmp_path)

    task_id = client.get("/v1/tasks", headers=_auth()).json()["tasks"][0]["task_id"]
    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()
    assert receipt["axes"]["decision_status"]["key"] == "verified"
    assert receipt["axes"]["evidence_strength"]["key"] == "verified"
    assert receipt["dimensions"]["evidence"]["checks_passed"] == 1
    # The touched file recorded on the section rides the Actions dimension.
    assert "src/login.py" in receipt["dimensions"]["actions"]["touched_files"]
