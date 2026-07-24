from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_chronicle.api import create_local_api_app
from agent_chronicle.control_plane import ControlStore
from agent_chronicle.control_web import render_control_body


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _registered_store(root: Path, *, sleep_seconds: float = 30.0) -> ControlStore:
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
        argv_template=[sys.executable, "-c", f"import time; time.sleep({sleep_seconds})"],
        idempotency_key="test:agent",
    )
    return store


def _create_attempt(client: TestClient, *, mutation_mode: str = "read_only") -> str:
    token = _csrf(client.get("/control").text)
    response = client.post(
        "/control/tasks",
        data={
            "csrf_token": token,
            "idempotency_key": f"web:test:create:{mutation_mode}",
            "task_id": "",
            "objective": f"Exercise {mutation_mode} control",
            "workspace_id": "workspace_test",
            "agent_id": "agent_test",
            "mutation_mode": mutation_mode,
            "success_checks": "owned process exits\nresult is recorded",
            "budget_policy_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    payload = client.get("/api/control").json()
    assert len(payload["attempts"]) == 1
    return str(payload["attempts"][0]["attempt_id"])


def test_control_get_is_empty_read_only_and_explains_authority_boundary(tmp_path):
    root = tmp_path / "state"
    app = create_local_api_app(store_dir=root)

    with TestClient(app) as client:
        page = client.get("/control")
        payload = client.get("/api/control").json()

    assert page.status_code == 200
    assert "Govern only what agentacct starts" in page.text
    assert "remain observed-only" in page.text
    assert "register-workspace" in page.text
    assert payload["summary"]["attempt_count"] == 0
    assert payload["authority_boundary"]["external_codex"] == "observed_only"
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


@pytest.mark.parametrize(
    ("execution_state", "shows_cancel"),
    [
        ("launching", False),
        ("running", True),
        ("cancel_requested", True),
    ],
)
def test_control_cancel_action_matches_supervisor_states(execution_state, shows_cancel):
    attempt_id = "attempt_test"
    body = render_control_body(
        {
            "summary": {},
            "supervisor": {},
            "tasks": [],
            "contracts": [
                {
                    "task_id": "task_test",
                    "objective": "Exercise cancel controls",
                }
            ],
            "agents": [
                {
                    "agent_id": "agent_test",
                    "display_name": "Test owned adapter",
                    "adapter": "local_argv",
                    "execution_backend": "subprocess",
                    "enabled": True,
                }
            ],
            "workspaces": [{"workspace_id": "workspace_test", "enabled": True}],
            "attempts": [
                {
                    "attempt_id": attempt_id,
                    "task_id": "task_test",
                    "agent_id": "agent_test",
                    "workspace_id": "workspace_test",
                    "execution_state": execution_state,
                    "outcome_state": "unknown",
                    "control_state": "ready",
                    "revision": 2,
                }
            ],
            "approvals": [],
            "budget_policies": [],
            "schedules": [],
            "observed_tasks": [],
        },
        csrf_token="csrf-test",
    )

    cancel_action = f'/control/attempts/{attempt_id}/cancel'
    assert (cancel_action in body) is shows_cancel


def test_control_task_form_lists_only_owned_subprocess_adapters_once():
    body = render_control_body(
        {
            "summary": {},
            "supervisor": {},
            "tasks": [],
            "contracts": [],
            "agents": [
                {
                    "agent_id": "agent_owned",
                    "display_name": "Owned agent",
                    "adapter": "local_argv",
                    "execution_backend": "subprocess",
                    "enabled": True,
                },
                {
                    "agent_id": "agent_external",
                    "display_name": "External observer",
                    "adapter": "paperclip",
                    "execution_backend": "external",
                    "enabled": True,
                },
            ],
            "workspaces": [{"workspace_id": "workspace_test", "enabled": True}],
            "attempts": [],
            "approvals": [],
            "budget_policies": [],
            "schedules": [],
            "observed_tasks": [],
        },
        csrf_token="csrf-test",
    )

    assert body.count('value="agent_owned"') == 1
    assert 'value="agent_external"' not in body
    assert "External observer" not in body


def test_control_mutations_require_csrf_and_expected_revision(tmp_path):
    root = tmp_path / "state"
    _registered_store(root)
    app = create_local_api_app(store_dir=root)

    with TestClient(app) as client:
        rejected = client.post(
            "/control/tasks",
            data={
                "idempotency_key": "web:test:no-csrf",
                "task_id": "",
                "objective": "Must not be created",
                "workspace_id": "workspace_test",
                "agent_id": "agent_test",
                "mutation_mode": "read_only",
                "success_checks": "",
                "budget_policy_id": "",
            },
        )
        assert rejected.status_code == 403
        attempt_id = _create_attempt(client)
        token = _csrf(client.get("/control").text)
        stale = client.post(
            f"/control/attempts/{attempt_id}/launch",
            data={
                "csrf_token": token,
                "idempotency_key": "web:test:stale-launch",
                "expected_revision": "999",
            },
            follow_redirects=False,
        )

    assert stale.status_code == 303
    assert "tone=error" in stale.headers["location"]
    assert ControlStore(root).project().attempts[attempt_id].execution_state == "pending"


def test_control_launch_cancel_and_task_detail_use_owned_attempt(tmp_path):
    root = tmp_path / "state"
    _registered_store(root)
    app = create_local_api_app(store_dir=root)

    with TestClient(app) as client:
        attempt_id = _create_attempt(client)
        before = client.get("/api/control").json()["attempts"][0]
        token = _csrf(client.get("/control").text)
        launched = client.post(
            f"/control/attempts/{attempt_id}/launch",
            data={
                "csrf_token": token,
                "idempotency_key": "web:test:launch",
                "expected_revision": str(before["revision"]),
            },
            follow_redirects=False,
        )
        assert launched.status_code == 303
        running = ControlStore(root).project().attempts[attempt_id]
        assert running.execution_state == "running"
        task_detail = client.get(f"/api/tasks/{running.task_id}")
        assert task_detail.status_code == 200
        assert task_detail.json()["states"]["execution"]["key"] == "running"
        token = _csrf(client.get("/control").text)
        cancelled = client.post(
            f"/control/attempts/{attempt_id}/cancel",
            data={
                "csrf_token": token,
                "idempotency_key": "web:test:cancel",
                "expected_revision": str(running.revision),
            },
            follow_redirects=False,
        )
        assert cancelled.status_code == 303
        terminal = ControlStore(root).project().attempts[attempt_id]
        assert terminal.execution_state == "cancelled"
        assert client.get(f"/tasks/{terminal.task_id}").status_code == 200


def test_workspace_write_is_held_until_single_use_approval_is_consumed(tmp_path):
    root = tmp_path / "state"
    _registered_store(root)
    app = create_local_api_app(store_dir=root)

    with TestClient(app) as client:
        attempt_id = _create_attempt(client, mutation_mode="workspace_write")
        projection = ControlStore(root).project()
        held = projection.attempts[attempt_id]
        approval = next(iter(projection.approvals.values()))
        assert held.control_state == "awaiting_approval"
        assert approval.state == "pending"

        token = _csrf(client.get("/control").text)
        blocked = client.post(
            f"/control/attempts/{attempt_id}/launch",
            data={
                "csrf_token": token,
                "idempotency_key": "web:test:held-launch",
                "expected_revision": str(held.revision),
            },
            follow_redirects=False,
        )
        assert "tone=error" in blocked.headers["location"]
        assert ControlStore(root).project().attempts[attempt_id].execution_state == "pending"

        token = _csrf(client.get("/control").text)
        approved = client.post(
            f"/control/approvals/{approval.approval_id}/decision",
            data={
                "csrf_token": token,
                "idempotency_key": "web:test:approve",
                "expected_revision": str(approval.revision),
                "decision": "approve",
            },
            follow_redirects=False,
        )
        assert approved.status_code == 303
        ready_projection = ControlStore(root).project()
        assert ready_projection.approvals[approval.approval_id].state == "consumed"
        assert ready_projection.attempts[attempt_id].control_state == "ready"

        # Replaying the exact decision is safe and does not consume twice.
        replay = client.post(
            f"/control/approvals/{approval.approval_id}/decision",
            data={
                "csrf_token": token,
                "idempotency_key": "web:test:approve",
                "expected_revision": str(approval.revision),
                "decision": "approve",
            },
            follow_redirects=False,
        )
        assert replay.status_code == 303
        assert ControlStore(root).project().approvals[approval.approval_id].state == "consumed"


def test_failed_approval_request_never_exposes_launch_and_has_safe_web_recovery(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "state"
    _registered_store(root)
    app = create_local_api_app(store_dir=root)
    original_request = ControlStore.request_approval

    def fail_request(*args, **kwargs):
        raise OSError("simulated approval request failure")

    monkeypatch.setattr(ControlStore, "request_approval", fail_request)
    with TestClient(app) as client:
        token = _csrf(client.get("/control").text)
        failed = client.post(
            "/control/tasks",
            data={
                "csrf_token": token,
                "idempotency_key": "web:test:create:request-failure",
                "task_id": "",
                "objective": "Remain held across request failure",
                "workspace_id": "workspace_test",
                "agent_id": "agent_test",
                "mutation_mode": "workspace_write",
                "success_checks": "approval must be durable",
                "budget_policy_id": "",
            },
            follow_redirects=False,
        )
        assert failed.status_code == 303
        assert "tone=error" in failed.headers["location"]

        interrupted = ControlStore(root).project()
        assert len(interrupted.attempts) == 1
        assert interrupted.approvals == {}
        attempt = next(iter(interrupted.attempts.values()))
        assert attempt.control_state == "awaiting_approval"
        page = client.get("/control").text
        assert f'/control/attempts/{attempt.attempt_id}/request-approval' in page
        assert f'/control/attempts/{attempt.attempt_id}/launch' not in page

        monkeypatch.setattr(ControlStore, "request_approval", original_request)
        token = _csrf(page)
        recovery_data = {
            "csrf_token": token,
            "idempotency_key": "web:test:recover-approval-request",
            "expected_revision": str(attempt.revision),
        }
        recovered = client.post(
            f"/control/attempts/{attempt.attempt_id}/request-approval",
            data=recovery_data,
            follow_redirects=False,
        )
        assert recovered.status_code == 303
        recovered_projection = ControlStore(root).project()
        assert len(recovered_projection.approvals) == 1
        approval = next(iter(recovered_projection.approvals.values()))
        assert approval.state == "pending"
        assert approval.attempt_id == attempt.attempt_id
        event_count = len(recovered_projection.events)

        replay = client.post(
            f"/control/attempts/{attempt.attempt_id}/request-approval",
            data=recovery_data,
            follow_redirects=False,
        )
        assert replay.status_code == 303
        assert len(ControlStore(root).project().events) == event_count
        recovered_page = client.get("/control").text
        assert f'/control/attempts/{attempt.attempt_id}/request-approval' not in recovered_page
        assert f'/control/attempts/{attempt.attempt_id}/launch' not in recovered_page
        assert f'/control/approvals/{approval.approval_id}/decision' in recovered_page


@pytest.mark.parametrize("failure_phase", ["before_append", "after_append"])
def test_approval_decision_reload_never_observes_half_resolved_state(
    tmp_path,
    monkeypatch,
    failure_phase,
):
    root = tmp_path / "state"
    _registered_store(root)
    app = create_local_api_app(store_dir=root)

    with TestClient(app) as client:
        attempt_id = _create_attempt(client, mutation_mode="workspace_write")
        before_projection = ControlStore(root).project()
        attempt = before_projection.attempts[attempt_id]
        approval = next(iter(before_projection.approvals.values()))
        before_count = len(before_projection.events)
        original_append = ControlStore._append_unlocked

        def fail_resolution_append(self, event):
            if event.action != "approval_resolved":
                return original_append(self, event)
            if failure_phase == "after_append":
                original_append(self, event)
            raise OSError(f"simulated {failure_phase} failure")

        monkeypatch.setattr(ControlStore, "_append_unlocked", fail_resolution_append)
        token = _csrf(client.get("/control").text)
        decision_data = {
            "csrf_token": token,
            "idempotency_key": "web:test:atomic-decision-failure",
            "expected_revision": str(approval.revision),
            "decision": "approve",
        }
        failed = client.post(
            f"/control/approvals/{approval.approval_id}/decision",
            data=decision_data,
            follow_redirects=False,
        )
        assert failed.status_code == 303
        assert "tone=error" in failed.headers["location"]

        reloaded = ControlStore(root).project()
        pair = (
            reloaded.approvals[approval.approval_id].state,
            reloaded.attempts[attempt.attempt_id].control_state,
        )
        assert pair == (
            ("pending", "awaiting_approval")
            if failure_phase == "before_append"
            else ("consumed", "ready")
        )
        interrupted_page = client.get("/control").text
        if failure_phase == "before_append":
            assert f'/control/approvals/{approval.approval_id}/decision' in interrupted_page
            assert f'/control/attempts/{attempt.attempt_id}/launch' not in interrupted_page
        else:
            assert f'/control/approvals/{approval.approval_id}/decision' not in interrupted_page
            assert f'/control/attempts/{attempt.attempt_id}/launch' in interrupted_page

        monkeypatch.setattr(ControlStore, "_append_unlocked", original_append)
        replay = client.post(
            f"/control/approvals/{approval.approval_id}/decision",
            data=decision_data,
            follow_redirects=False,
        )
        assert replay.status_code == 303
        final_projection = ControlStore(root).project()
        assert final_projection.approvals[approval.approval_id].state == "consumed"
        assert final_projection.attempts[attempt.attempt_id].control_state == "ready"
        assert len(final_projection.events) == before_count + 1


def test_owned_cancel_does_not_signal_an_external_process(tmp_path):
    import subprocess

    root = tmp_path / "state"
    _registered_store(root)
    external = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    app = create_local_api_app(store_dir=root)
    try:
        with TestClient(app) as client:
            attempt_id = _create_attempt(client)
            pending = ControlStore(root).project().attempts[attempt_id]
            token = _csrf(client.get("/control").text)
            client.post(
                f"/control/attempts/{attempt_id}/launch",
                data={
                    "csrf_token": token,
                    "idempotency_key": "web:test:owned-launch",
                    "expected_revision": str(pending.revision),
                },
                follow_redirects=False,
            )
            running = ControlStore(root).project().attempts[attempt_id]
            token = _csrf(client.get("/control").text)
            client.post(
                f"/control/attempts/{attempt_id}/cancel",
                data={
                    "csrf_token": token,
                    "idempotency_key": "web:test:owned-cancel",
                    "expected_revision": str(running.revision),
                },
                follow_redirects=False,
            )
            assert external.poll() is None
    finally:
        external.terminate()
        try:
            external.wait(timeout=3)
        except subprocess.TimeoutExpired:
            external.kill()
            external.wait(timeout=3)


def test_terminal_retry_creates_and_launches_a_fresh_attempt(tmp_path):
    root = tmp_path / "state"
    _registered_store(root, sleep_seconds=0.05)
    app = create_local_api_app(store_dir=root)

    with TestClient(app) as client:
        attempt_id = _create_attempt(client)
        pending = ControlStore(root).project().attempts[attempt_id]
        token = _csrf(client.get("/control").text)
        client.post(
            f"/control/attempts/{attempt_id}/launch",
            data={
                "csrf_token": token,
                "idempotency_key": "web:test:quick-launch",
                "expected_revision": str(pending.revision),
            },
            follow_redirects=False,
        )
        for _ in range(100):
            terminal = ControlStore(root).project().attempts[attempt_id]
            if terminal.execution_state == "succeeded":
                break
            time.sleep(0.01)
        assert terminal.execution_state == "succeeded"

        token = _csrf(client.get("/control").text)
        retried = client.post(
            f"/control/attempts/{attempt_id}/retry",
            data={
                "csrf_token": token,
                "idempotency_key": "web:test:retry",
                "expected_revision": str(terminal.revision),
            },
            follow_redirects=False,
        )
        assert retried.status_code == 303
        attempts = ControlStore(root).project().attempts
        assert len(attempts) == 2
        fresh = next(row for key, row in attempts.items() if key != attempt_id)
        assert fresh.execution_state in {"running", "succeeded"}
