from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.control_plane import ControlStore


def _registered_store(root: Path) -> ControlStore:
    store = ControlStore(root)
    store.register_workspace(
        root.parent,
        workspace_id="workspace_test",
        store_dir=root,
        idempotency_key="test:workspace",
    )
    store.register_agent(
        "agent_test",
        display_name="Test owned adapter",
        adapter="local_argv",
        execution_backend="subprocess",
        argv_template=[sys.executable, "-c", "import time; time.sleep(30)"],
        idempotency_key="test:agent",
    )
    return store


def test_control_get_is_empty_read_only_and_reports_authority_boundary(tmp_path):
    root = tmp_path / "state"
    app = create_local_api_app(store_dir=root)

    with TestClient(app) as client:
        response = client.get("/api/control")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["attempt_count"] == 0
    assert payload["authority_boundary"]["external_codex"] == "observed_only"
    # A read never materializes control-plane state on disk.
    assert not (root / "control-plane").exists()


def test_control_json_omits_process_authority_paths_and_argv(tmp_path):
    root = tmp_path / "state"
    store = _registered_store(root)
    task = store.create_task(origin="planned", idempotency_key="test:task")
    contract = store.create_contract(
        task.task_id,
        objective="Sanitize the product projection",
        workspace_id="workspace_test",
        permission_envelope={"mutation_mode": "read_only", "secret": "do-not-render"},
        idempotency_key="test:contract",
    )
    store.create_attempt(
        task.task_id,
        agent_id="agent_test",
        workspace_id="workspace_test",
        contract_revision=contract.revision,
        idempotency_key="test:attempt",
    )
    app = create_local_api_app(store_dir=root)

    with TestClient(app) as client:
        response = client.get("/api/control")

    encoded = response.text
    assert response.status_code == 200
    for forbidden in (
        "canonical_root",
        "store_dir",
        "argv_template",
        "process_group_id",
        "process_birth_time",
        "process_executable",
        "process_cwd",
        "ownership_nonce_hash",
        "manifest_id",
        "do-not-render",
        str(tmp_path),
    ):
        assert forbidden not in encoded
    assert response.json()["agents"][0]["command_registered"] is True


def test_control_json_reports_workspace_write_as_launch_approval_required(tmp_path):
    root = tmp_path / "state"
    store = _registered_store(root)
    task = store.create_task(origin="planned", idempotency_key="test:approval-task")
    store.create_contract(
        task.task_id,
        objective="Require approval for workspace mutation",
        workspace_id="workspace_test",
        permission_envelope={"mutation_mode": "workspace_write"},
        idempotency_key="test:approval-contract",
    )
    app = create_local_api_app(store_dir=root)

    with TestClient(app) as client:
        response = client.get("/api/control")

    assert response.status_code == 200
    assert response.json()["contracts"][0]["launch_approval_required"] is True
