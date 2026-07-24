from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from click.utils import strip_ansi
from typer.testing import CliRunner

from agent_chronicle.cli import app
from agent_chronicle.control_plane import ControlStore
from agent_chronicle.supervisor import OwnedSupervisor


runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, ["control", *args])


def _register_foundation(tmp_path: Path, source: str) -> tuple[Path, Path, dict[str, object]]:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "owned_agent.py"
    script.write_text(source, encoding="utf-8")

    workspace_result = _invoke(
        "register-workspace",
        "--store-dir",
        str(state),
        "--root",
        str(workspace),
        "--workspace-id",
        "workspace_test",
        "--json",
    )
    assert workspace_result.exit_code == 0, workspace_result.output
    assert str(workspace) not in workspace_result.output
    assert str(state) not in workspace_result.output

    agent_result = _invoke(
        "register-agent",
        "--store-dir",
        str(state),
        "--agent-id",
        "agent_test",
        "--display-name",
        "Owned test agent",
        "--argv-json",
        json.dumps([sys.executable, str(script)]),
        "--json",
    )
    assert agent_result.exit_code == 0, agent_result.output
    assert str(script) not in agent_result.output
    assert str(sys.executable) not in agent_result.output

    plan_result = _invoke(
        "plan",
        "--store-dir",
        str(state),
        "--objective",
        "Exercise the owned control path",
        "--workspace-id",
        "workspace_test",
        "--agent-id",
        "agent_test",
        "--permission-envelope-json",
        json.dumps({"mutation_mode": "read_only", "private_path": str(tmp_path)}),
        "--success-check",
        "exit_code_zero",
        "--json",
    )
    assert plan_result.exit_code == 0, plan_result.output
    assert str(tmp_path) not in plan_result.output
    assert "private_path" not in plan_result.output
    return state, workspace, json.loads(plan_result.output)


def _wait_for_state(store: ControlStore, attempt_id: str, states: set[str], timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        attempt = store.project().attempts[attempt_id]
        if attempt.execution_state in states:
            return attempt
        time.sleep(0.02)
    return store.project().attempts[attempt_id]


def test_control_status_requires_an_explicit_store_dir() -> None:
    result = _invoke("status", "--json")

    assert result.exit_code == 2
    assert "--store-dir" in strip_ansi(result.output)


def test_register_agent_requires_a_strict_json_argv_array(tmp_path: Path) -> None:
    result = _invoke(
        "register-agent",
        "--store-dir",
        str(tmp_path / "state"),
        "--agent-id",
        "agent_test",
        "--display-name",
        "Test",
        "--argv-json",
        "python -c 'print(1)'",
        "--json",
    )

    assert result.exit_code == 2
    assert "--argv-json must be valid JSON" in strip_ansi(result.output)
    assert not (tmp_path / "state" / "control-plane").exists()


def test_plan_is_idempotent_and_projection_redacts_authority_material(tmp_path: Path) -> None:
    state, workspace, first = _register_foundation(tmp_path, "print('DONE', flush=True)\n")
    event_count = len(ControlStore(state).project().events)

    second_result = _invoke(
        "plan",
        "--store-dir",
        str(state),
        "--objective",
        "Exercise the owned control path",
        "--workspace-id",
        "workspace_test",
        "--agent-id",
        "agent_test",
        "--permission-envelope-json",
        json.dumps({"mutation_mode": "read_only", "private_path": str(tmp_path)}),
        "--success-check",
        "exit_code_zero",
        "--json",
    )
    status_result = _invoke("status", "--store-dir", str(state), "--json")

    assert second_result.exit_code == 0, second_result.output
    second = json.loads(second_result.output)
    assert second["task"]["task_id"] == first["task"]["task_id"]
    assert second["attempt"]["attempt_id"] == first["attempt"]["attempt_id"]
    assert len(ControlStore(state).project().events) == event_count
    assert status_result.exit_code == 0, status_result.output
    status = status_result.output
    assert str(workspace) not in status
    assert str(tmp_path) not in status
    assert str(sys.executable) not in status
    assert "owned_agent.py" not in status
    assert "private_path" not in status
    assert "ownership_nonce" not in status
    assert "capabilities" not in status
    assert '"reason"' not in status


@pytest.mark.parametrize(
    "permission_envelope",
    [
        {"mutation_mode": "workspace_write"},
        {"launch_approval_required": True},
    ],
)
def test_plan_holds_approval_required_attempt_and_launch_cannot_bypass(
    tmp_path: Path,
    permission_envelope: dict[str, object],
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "must-not-run"
    script = workspace / "agent.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
    assert _invoke(
        "register-workspace",
        "--store-dir",
        str(state),
        "--root",
        str(workspace),
        "--workspace-id",
        "workspace_approval",
        "--json",
    ).exit_code == 0
    assert _invoke(
        "register-agent",
        "--store-dir",
        str(state),
        "--agent-id",
        "agent_approval",
        "--display-name",
        "Approval test agent",
        "--argv-json",
        json.dumps([sys.executable, str(script)]),
        "--json",
    ).exit_code == 0
    plan = _invoke(
        "plan",
        "--store-dir",
        str(state),
        "--objective",
        "Mutate only after approval",
        "--workspace-id",
        "workspace_approval",
        "--agent-id",
        "agent_approval",
        "--permission-envelope-json",
        json.dumps(permission_envelope),
        "--json",
    )
    assert plan.exit_code == 0, plan.output
    payload = json.loads(plan.output)
    assert payload["launch_approval_required"] is True
    assert payload["attempt"]["control_state"] == "awaiting_approval"
    assert "Request launch approval" in payload["next_step"]

    launch = _invoke(
        "launch",
        "--store-dir",
        str(state),
        "--attempt-id",
        str(payload["attempt"]["attempt_id"]),
        "--expected-revision",
        str(payload["attempt"]["revision"]),
        "--json",
    )

    assert launch.exit_code == 1
    assert "awaiting_approval" in launch.output
    assert not marker.exists()
    projected = ControlStore(state).project().attempts[str(payload["attempt"]["attempt_id"])]
    assert projected.execution_state == "pending"
    assert projected.control_state == "awaiting_approval"

    requested = _invoke(
        "request-approval",
        "--store-dir",
        str(state),
        "--task-id",
        str(payload["task"]["task_id"]),
        "--attempt-id",
        str(payload["attempt"]["attempt_id"]),
        "--expected-attempt-revision",
        str(payload["attempt"]["revision"]),
        "--kind",
        "workspace_write",
        "--requested-action",
        "launch",
        "--expires-at",
        str(time.time() + 120),
        "--json",
    )
    assert requested.exit_code == 0, requested.output
    requested_payload = json.loads(requested.output)
    decided = _invoke(
        "decide-approval",
        "--store-dir",
        str(state),
        "--approval-id",
        str(requested_payload["approval"]["approval_id"]),
        "--decision",
        "approve",
        "--expected-revision",
        str(requested_payload["approval"]["revision"]),
        "--json",
    )
    assert decided.exit_code == 0, decided.output
    decision_payload = json.loads(decided.output)
    assert decision_payload["approval"]["state"] == "consumed"
    assert decision_payload["attempt"]["control_state"] == "ready"

    approved_launch = _invoke(
        "launch",
        "--store-dir",
        str(state),
        "--attempt-id",
        str(payload["attempt"]["attempt_id"]),
        "--expected-revision",
        str(decision_payload["attempt"]["revision"]),
        "--json",
    )
    assert approved_launch.exit_code == 0, approved_launch.output
    assert json.loads(approved_launch.output)["attempt"]["execution_state"] == "succeeded"
    assert marker.read_text(encoding="utf-8") == "ran"


def test_registration_update_requires_the_expected_revision(tmp_path: Path) -> None:
    state, workspace, _plan = _register_foundation(tmp_path, "print('DONE')\n")

    result = _invoke(
        "register-workspace",
        "--store-dir",
        str(state),
        "--root",
        str(workspace),
        "--workspace-id",
        "workspace_test",
        "--disabled",
        "--expected-revision",
        "0",
        "--idempotency-key",
        "workspace:disable-stale",
        "--json",
    )

    assert result.exit_code == 1
    assert "revision is 1, expected 0" in result.output
    assert "Traceback" not in result.output


def test_owned_launch_and_failed_retry_create_a_new_pending_attempt(tmp_path: Path) -> None:
    state, _workspace, plan = _register_foundation(tmp_path, "raise SystemExit(7)\n")
    attempt_id = str(plan["attempt"]["attempt_id"])

    launch = _invoke(
        "launch",
        "--store-dir",
        str(state),
        "--attempt-id",
        attempt_id,
        "--expected-revision",
        "1",
        "--idempotency-key",
        "launch:test-failure",
        "--json",
    )
    assert launch.exit_code == 0, launch.output
    assert "run_dir" not in launch.output
    assert "manifest" not in launch.output
    assert "nonce" not in launch.output

    store = ControlStore(state)
    failed = _wait_for_state(store, attempt_id, {"failed"})
    assert failed.execution_state == "failed"
    task_id = str(plan["task"]["task_id"])
    first_contract = store.project().contracts[task_id]
    second_contract = store.create_contract(
        task_id,
        objective="Retry under the current contract",
        workspace_id=first_contract.workspace_id,
        permission_envelope={"mutation_mode": "workspace_write"},
        budget_policy_ids=first_contract.budget_policy_ids,
        success_checks=first_contract.success_checks,
        expected_revision=first_contract.revision,
        idempotency_key="contract:retry-current",
    )
    retry = _invoke(
        "retry",
        "--store-dir",
        str(state),
        "--attempt-id",
        attempt_id,
        "--expected-revision",
        str(failed.revision),
        "--idempotency-key",
        "retry:test-failure",
        "--json",
    )
    store.create_contract(
        task_id,
        objective="A later contract must not change an idempotent retry snapshot",
        workspace_id=second_contract.workspace_id,
        permission_envelope=second_contract.permission_envelope,
        budget_policy_ids=second_contract.budget_policy_ids,
        success_checks=second_contract.success_checks,
        expected_revision=second_contract.revision,
        idempotency_key="contract:retry-later",
    )
    replay = _invoke(
        "retry",
        "--store-dir",
        str(state),
        "--attempt-id",
        attempt_id,
        "--expected-revision",
        str(failed.revision),
        "--idempotency-key",
        "retry:test-failure",
        "--json",
    )

    assert retry.exit_code == 0, retry.output
    assert replay.exit_code == 0, replay.output
    created = json.loads(retry.output)["attempt"]
    assert created["attempt_id"] != attempt_id
    assert created["execution_state"] == "pending"
    assert created["control_state"] == "awaiting_approval"
    assert created["contract_revision"] == second_contract.revision
    assert json.loads(retry.output)["launch_approval_required"] is True
    assert "Request launch approval" in json.loads(retry.output)["next_step"]
    assert json.loads(replay.output)["attempt"] == created


def test_foreground_launch_timeout_cancels_instead_of_orphaning(tmp_path: Path) -> None:
    state, _workspace, plan = _register_foundation(
        tmp_path,
        "import time\nwhile True:\n    time.sleep(0.1)\n",
    )
    attempt_id = str(plan["attempt"]["attempt_id"])

    launch = _invoke(
        "launch",
        "--store-dir",
        str(state),
        "--attempt-id",
        attempt_id,
        "--expected-revision",
        "1",
        "--timeout-seconds",
        "0.1",
        "--idempotency-key",
        "launch:test-timeout",
        "--json",
    )

    assert launch.exit_code == 0, launch.output
    payload = json.loads(launch.output)
    assert payload["foreground_supervised"] is True
    assert payload["timed_out"] is True
    assert payload["attempt"]["execution_state"] == "cancelled"
    assert ControlStore(state).project().attempts[attempt_id].execution_state == "cancelled"


def test_cancel_controls_only_the_exact_owned_attempt(tmp_path: Path) -> None:
    state, _workspace, plan = _register_foundation(
        tmp_path,
        "import time\nprint('READY', flush=True)\nwhile True:\n    time.sleep(0.1)\n",
    )
    attempt_id = str(plan["attempt"]["attempt_id"])
    store = ControlStore(state)
    owner = OwnedSupervisor(state, control_store=store)
    launched = owner.launch_attempt(attempt_id, idempotency_key="launch:test-cancel").attempt
    owner.close()
    try:
        cancel = _invoke(
            "cancel",
            "--store-dir",
            str(state),
            "--attempt-id",
            attempt_id,
            "--expected-revision",
            str(launched.revision),
            "--idempotency-key",
            "cancel:test-owned",
            "--json",
        )
        assert cancel.exit_code == 0, cancel.output
        assert json.loads(cancel.output)["attempt"]["execution_state"] == "cancelled"
    finally:
        current = store.project().attempts[attempt_id]
        if current.execution_state in {"running", "cancel_requested"}:
            cleanup = OwnedSupervisor(state, control_store=store, cancel_grace_seconds=0)
            try:
                cleanup.cancel_attempt(attempt_id, idempotency_key="cancel:test-cleanup")
            finally:
                cleanup.close()


def test_external_or_observed_agent_attempt_is_read_only_at_cli_boundary(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ControlStore(state)
    task = store.create_task(origin="planned", idempotency_key="task:external")
    registered_workspace = store.register_workspace(
        workspace,
        workspace_id="workspace_external",
        idempotency_key="workspace:external",
    )
    agent = store.register_agent(
        "agent_external",
        display_name="Observed Paperclip agent",
        adapter="paperclip",
        execution_backend="external",
        argv_template=[sys.executable, "-c", "print('MUST_NOT_RUN')"],
        capabilities=["secret-capability"],
        idempotency_key="agent:external",
    )
    contract = store.create_contract(
        task.task_id,
        objective="Observed task",
        workspace_id=registered_workspace.workspace_id,
        permission_envelope={"secret_path": str(tmp_path)},
        idempotency_key="contract:external",
    )
    attempt = store.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=registered_workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:external",
    )

    launch = _invoke(
        "launch",
        "--store-dir",
        str(state),
        "--attempt-id",
        attempt.attempt_id,
        "--expected-revision",
        str(attempt.revision),
        "--json",
    )
    status = _invoke("status", "--store-dir", str(state), "--json")

    assert launch.exit_code == 1
    assert "external or observed-only" in launch.output
    assert store.project().attempts[attempt.attempt_id].execution_state == "pending"
    assert status.exit_code == 0
    assert "secret-capability" not in status.output
    assert "secret_path" not in status.output
    assert str(tmp_path) not in status.output


def test_budget_schedule_and_approval_commands_are_revisioned_and_idempotent(tmp_path: Path) -> None:
    state, _workspace, plan = _register_foundation(tmp_path, "print('DONE')\n")
    task_id = str(plan["task"]["task_id"])
    policy = _invoke(
        "register-budget-policy",
        "--store-dir",
        str(state),
        "--policy-id",
        "policy_cost",
        "--scope",
        "task",
        "--metric",
        "cost_usd",
        "--limit",
        "5",
        "--basis",
        "provider_billed",
        "--action",
        "cancel",
        "--idempotency-key",
        "budget:test",
        "--json",
    )
    assert policy.exit_code == 0, policy.output

    next_run_at = time.time() + 300
    schedule = _invoke(
        "register-schedule",
        "--store-dir",
        str(state),
        "--task-id",
        task_id,
        "--cadence",
        "fixed",
        "--next-run-at",
        str(next_run_at),
        "--interval-seconds",
        "60",
        "--idempotency-key",
        "schedule:test",
        "--json",
    )
    assert schedule.exit_code == 0, schedule.output

    expires_at = time.time() + 120
    requested = _invoke(
        "request-approval",
        "--store-dir",
        str(state),
        "--task-id",
        task_id,
        "--attempt-id",
        str(plan["attempt"]["attempt_id"]),
        "--expected-attempt-revision",
        str(plan["attempt"]["revision"]),
        "--kind",
        "dirty_workspace",
        "--requested-action",
        "launch",
        "--expires-at",
        str(expires_at),
        "--idempotency-key",
        "approval:request-test",
        "--json",
    )
    assert requested.exit_code == 0, requested.output
    requested_payload = json.loads(requested.output)
    approval = requested_payload["approval"]
    held_attempt = requested_payload["attempt"]
    assert held_attempt["control_state"] == "awaiting_approval"
    decision_args = (
        "decide-approval",
        "--store-dir",
        str(state),
        "--approval-id",
        str(approval["approval_id"]),
        "--decision",
        "approve",
        "--expected-revision",
        str(approval["revision"]),
        "--idempotency-key",
        "approval:decide-test",
        "--json",
    )
    decided = _invoke(*decision_args)
    replay = _invoke(*decision_args)

    assert decided.exit_code == 0, decided.output
    assert replay.exit_code == 0, replay.output
    decision_payload = json.loads(decided.output)
    approved = decision_payload["approval"]
    released = decision_payload["attempt"]
    assert approved["state"] == "consumed"
    assert released["control_state"] == "ready"
    assert json.loads(replay.output) == decision_payload
    projected = ControlStore(state).project()
    assert projected.approvals[str(approval["approval_id"])].state == "consumed"
    assert projected.attempts[str(plan["attempt"]["attempt_id"])].control_state == "ready"

    launch = _invoke(
        "launch",
        "--store-dir",
        str(state),
        "--attempt-id",
        str(plan["attempt"]["attempt_id"]),
        "--expected-revision",
        str(released["revision"]),
        "--idempotency-key",
        "launch:approved-test",
        "--json",
    )
    assert launch.exit_code == 0, launch.output
    assert json.loads(launch.output)["attempt"]["execution_state"] == "succeeded"


def test_reconcile_returns_only_sanitized_attempt_ids(tmp_path: Path) -> None:
    state, _workspace, _plan = _register_foundation(tmp_path, "print('DONE')\n")

    result = _invoke("reconcile", "--store-dir", str(state), "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mutation_idempotency"] == "supervisor-internal"
    assert set(payload["classification"]) == {
        "legacy_marked_lost",
        "marked_lost",
        "verified_live_owned",
    }
    assert payload["persistent_monitoring"] is False
    assert "nonce" not in result.output
    assert str(tmp_path) not in result.output
