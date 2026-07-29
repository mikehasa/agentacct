from __future__ import annotations

import json
import os
import signal
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest

from agentacct.control_plane import ControlStore
from agentacct.storage import OWNERSHIP_SCHEMA_VERSION, RunStore
from agentacct.supervisor import OwnedSupervisor, SupervisorAlreadyRunning, SupervisorError


def _attempt(
    tmp_path,
    source: str,
    *,
    direct_executable: bool = False,
    mutation_mode: str = "read_only",
    adapter: str = "local_argv",
    execution_backend: str = "subprocess",
):
    state = tmp_path / "state"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    script = workspace_root / "agent.py"
    if direct_executable:
        script.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        script.chmod(0o700)
        argv = [str(script)]
    else:
        script.write_text(source, encoding="utf-8")
        argv = [sys.executable, str(script)]
    control = ControlStore(state)
    task = control.create_task(origin="planned", idempotency_key="task:create")
    workspace = control.register_workspace(
        workspace_root,
        store_dir=state,
        idempotency_key="workspace:create",
    )
    agent = control.register_agent(
        "agent_python",
        display_name="Python test agent",
        adapter=adapter,
        execution_backend=execution_backend,
        argv_template=argv,
        idempotency_key="agent:create",
    )
    policy = control.register_budget_policy(
        "policy_test",
        scope="task",
        metric="cost_usd",
        limit=5,
        basis="provider_billed",
        action="cancel",
        idempotency_key="budget:create",
    )
    contract = control.create_contract(
        task.task_id,
        objective="Run the test agent",
        workspace_id=workspace.workspace_id,
        permission_envelope={"mutation_mode": mutation_mode},
        budget_policy_ids=[policy.policy_id],
        success_checks=["exit_code_zero"],
        idempotency_key="contract:create",
    )
    attempt = control.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:create",
    )
    return state, control, attempt


def test_supervisor_launches_registered_argv_freezes_manifest_and_monitors_exit(tmp_path):
    state, control, attempt = _attempt(tmp_path, "print('SUPERVISOR_OK', flush=True)\n")
    supervisor = OwnedSupervisor(state)

    launched = supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:one")
    finished = supervisor.wait(attempt.attempt_id)

    assert launched.attempt.execution_state == "running"
    assert finished.execution_state == "succeeded"
    assert finished.exit_code == 0
    assert launched.manifest_path.is_file()
    assert stat.S_IMODE(launched.manifest_path.stat().st_mode) == 0o600
    assert (launched.run_dir / "stdout.log").read_text().strip() == "SUPERVISOR_OK"
    metadata = RunStore(state).read_metadata(attempt.attempt_id)
    assert metadata["ownership_schema_version"] == OWNERSHIP_SCHEMA_VERSION
    assert metadata["process_birth_time"] > 0
    assert metadata["process_group_id"] == metadata["pid"]
    assert metadata["process_cwd"] == str(tmp_path / "workspace")
    assert metadata["ownership_nonce"]
    manifest = control.read_manifest(metadata["manifest_id"])
    assert manifest["payload"]["environment_key_names"]
    assert manifest["payload"]["budget_policies"][0]["basis"] == "provider_billed"
    assert manifest["payload"]["budget_policies"][0]["revision"] == 1
    assert "ownership_nonce" not in json.dumps(manifest)
    supervisor.close()


def test_launch_commit_failure_never_releases_target_process(tmp_path, monkeypatch):
    marker = tmp_path / "target-ran"
    state, control, attempt = _attempt(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
    )
    original_transition = control.transition_attempt

    def fail_running_transition(*args, **kwargs):
        if kwargs.get("execution_state") == "running":
            raise OSError("simulated durable transition failure")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(control, "transition_attempt", fail_running_transition)
    supervisor = OwnedSupervisor(state, control_store=control)

    try:
        with pytest.raises(SupervisorError, match="launch failed"):
            supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:commit-failure")
        metadata = RunStore(state).read_metadata(attempt.attempt_id)
        assert control.project().attempts[attempt.attempt_id].execution_state == "failed"
        assert metadata["status"] == "failed"
        assert not marker.exists()
        assert not psutil.pid_exists(metadata["pid"])
    finally:
        supervisor.close()


@pytest.mark.parametrize(
    ("adapter", "execution_backend"),
    [("remote_rpc", "subprocess"), ("local_argv", "container")],
)
def test_preflight_executes_only_local_argv_subprocess_agents(
    tmp_path,
    adapter,
    execution_backend,
):
    state, control, attempt = _attempt(
        tmp_path,
        "raise AssertionError('unsupported adapter executed')\n",
        adapter=adapter,
        execution_backend=execution_backend,
    )
    supervisor = OwnedSupervisor(state)

    try:
        with pytest.raises(SupervisorError, match="supported local subprocess adapter"):
            supervisor.preflight(attempt.attempt_id)
        assert control.project().attempts[attempt.attempt_id].execution_state == "pending"
        assert not RunStore(state).run_dir(attempt.attempt_id).exists()
    finally:
        supervisor.close()


def test_approved_attempt_cannot_launch_after_agent_command_revision_changes(tmp_path):
    marker = tmp_path / "updated-command-ran"
    state, control, attempt = _attempt(
        tmp_path,
        "print('original command')\n",
        mutation_mode="workspace_write",
    )
    approval = control.request_approval(
        attempt.task_id,
        attempt_id=attempt.attempt_id,
        kind="workspace_mutation",
        requested_action="launch",
        requested_by="policy",
        expires_at=time.time() + 60,
        idempotency_key="approval:agent-revision",
    )
    control.resolve_approval_for_attempt(
        approval.approval_id,
        approve=True,
        decided_by="local-user",
        expected_revision=approval.revision,
        idempotency_key="approval:agent-revision:resolve",
    )
    current_agent = control.project().agents[attempt.agent_id]
    replacement = tmp_path / "workspace" / "replacement.py"
    replacement.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    updated_agent = control.register_agent(
        current_agent.agent_id,
        display_name=current_agent.display_name,
        adapter=current_agent.adapter,
        execution_backend=current_agent.execution_backend,
        argv_template=[sys.executable, str(replacement)],
        capabilities=current_agent.capabilities,
        enabled=True,
        expected_revision=current_agent.revision,
        idempotency_key="agent:update-after-approval",
    )
    supervisor = OwnedSupervisor(state)

    try:
        assert attempt.agent_revision < updated_agent.revision
        with pytest.raises(SupervisorError, match="agent revision is unavailable"):
            supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:stale-agent-revision")
        assert not marker.exists()
        assert control.project().attempts[attempt.attempt_id].execution_state == "pending"
    finally:
        supervisor.close()


def test_lease_is_fenced_immediately_before_launch_gate_release(tmp_path, monkeypatch):
    marker = tmp_path / "target-ran-after-takeover"
    state, control, attempt = _attempt(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
    )
    supervisor = OwnedSupervisor(state)
    original_assert = supervisor._assert_preflight_identities
    identity_checks = 0

    def force_takeover_at_gate(manifest_payload):
        nonlocal identity_checks
        identity_checks += 1
        original_assert(manifest_payload)
        if identity_checks == 4:
            with supervisor.lease._locked():
                lease_state = supervisor.lease._read()
                assert lease_state is not None
                lease_state.update({"lease_id": "supervisor_takeover", "pid": 99999999})
                supervisor.lease._write(lease_state)

    monkeypatch.setattr(supervisor, "_assert_preflight_identities", force_takeover_at_gate)

    try:
        with pytest.raises(SupervisorError, match="launch failed"):
            supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:takeover-at-gate")
        assert identity_checks == 4
        assert not marker.exists()
        metadata = RunStore(state).read_metadata(attempt.attempt_id)
        deadline = time.time() + 2
        while psutil.pid_exists(metadata["pid"]) and time.time() < deadline:
            time.sleep(0.02)
        assert not psutil.pid_exists(metadata["pid"])
        assert control.project().attempts[attempt.attempt_id].execution_state == "running"
    finally:
        supervisor.close()


def test_takeover_at_exact_gate_waits_until_authorized_release(tmp_path, monkeypatch):
    marker = tmp_path / "authorized-target-ran"
    state, _control, attempt = _attempt(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
    )
    owner = OwnedSupervisor(state)
    contender = OwnedSupervisor(state)
    gate_entered = threading.Event()
    takeover_started = threading.Event()
    takeover_done = threading.Event()
    takeover_outcome: list[BaseException | str] = []
    original_gate_write = owner.lease._write_gate_byte

    def pause_inside_atomic_gate(descriptor):
        gate_entered.set()
        assert takeover_started.wait(timeout=2)
        # The contender has begun acquire() but cannot inspect or replace the
        # lease while the owner still holds the lease flock around this write.
        time.sleep(0.05)
        assert not takeover_done.is_set()
        original_gate_write(descriptor)

    def try_takeover():
        assert gate_entered.wait(timeout=2)
        takeover_started.set()
        try:
            contender.start()
            takeover_outcome.append("acquired")
        except BaseException as exc:  # surfaced and asserted by the test thread
            takeover_outcome.append(exc)
        finally:
            takeover_done.set()

    monkeypatch.setattr(owner.lease, "_write_gate_byte", pause_inside_atomic_gate)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(try_takeover)
            owner.launch_attempt(attempt.attempt_id, idempotency_key="launch:atomic-gate")
            future.result(timeout=3)
        assert takeover_done.is_set()
        assert len(takeover_outcome) == 1
        assert isinstance(takeover_outcome[0], SupervisorAlreadyRunning)
        assert owner.wait(attempt.attempt_id).execution_state == "succeeded"
        assert marker.read_text() == "ran"
    finally:
        contender.close()
        owner.close()


def test_supervisor_cancels_only_after_revalidating_owned_process(tmp_path):
    state, control, attempt = _attempt(
        tmp_path,
        "import time\nprint('READY', flush=True)\nwhile True:\n    time.sleep(0.1)\n",
    )
    supervisor = OwnedSupervisor(state, cancel_grace_seconds=0.1)
    launched = supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:cancel")
    deadline = time.time() + 3
    while "READY" not in (launched.run_dir / "stdout.log").read_text(errors="replace") and time.time() < deadline:
        time.sleep(0.02)

    cancelled = supervisor.cancel_attempt(attempt.attempt_id, idempotency_key="cancel:one")

    assert cancelled.execution_state == "cancelled"
    assert RunStore(state).read_metadata(attempt.attempt_id)["status"] == "cancelled"
    supervisor.close()


def test_direct_shebang_agent_keeps_verifiable_shim_identity(tmp_path):
    state, _control, attempt = _attempt(
        tmp_path,
        "import time\nprint('READY', flush=True)\nwhile True:\n    time.sleep(0.1)\n",
        direct_executable=True,
    )
    supervisor = OwnedSupervisor(state, cancel_grace_seconds=0.05)
    launched = supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:shebang")

    try:
        deadline = time.time() + 3
        while "READY" not in launched.run_dir.joinpath("stdout.log").read_text(errors="replace"):
            assert time.time() < deadline
            time.sleep(0.02)
        metadata = RunStore(state).verify_owned_process(attempt.attempt_id)
        assert metadata["process_executable"] == str(Path(sys.executable).resolve())
        assert supervisor.cancel_attempt(
            attempt.attempt_id,
            idempotency_key="cancel:shebang",
        ).execution_state == "cancelled"
    finally:
        supervisor.close()


def test_cancel_kills_same_group_child_that_ignores_sigterm(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    child_source = (
        "import os,signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(child_pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "while True: time.sleep(0.1)\n"
    )
    parent_source = (
        "import subprocess,sys,time\n"
        f"p=subprocess.Popen([sys.executable, '-c', {child_source!r}])\n"
        f"deadline=time.time()+3\nwhile not __import__('pathlib').Path({str(child_pid_path)!r}).exists():\n"
        "    assert time.time() < deadline\n    time.sleep(0.01)\n"
        "print('READY', flush=True)\n"
        "while True: time.sleep(0.1)\n"
    )
    state, _control, attempt = _attempt(tmp_path, parent_source)
    supervisor = OwnedSupervisor(state, cancel_grace_seconds=0.05)
    launched = supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:descendant")

    try:
        deadline = time.time() + 3
        while not child_pid_path.exists():
            assert time.time() < deadline
            time.sleep(0.02)
        child_pid = int(child_pid_path.read_text())
        cancelled = supervisor.cancel_attempt(attempt.attempt_id, idempotency_key="cancel:descendant")
        assert cancelled.execution_state == "cancelled"
        deadline = time.time() + 3
        while psutil.pid_exists(child_pid) and time.time() < deadline:
            try:
                if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.02)
        assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        assert launched.attempt.process_group_id == RunStore(state).read_metadata(attempt.attempt_id)["process_group_id"]
    finally:
        supervisor.close()


def test_cancel_fails_closed_when_process_fingerprint_is_tampered(tmp_path):
    state, _control, attempt = _attempt(
        tmp_path,
        "import time\nwhile True:\n    time.sleep(0.1)\n",
    )
    supervisor = OwnedSupervisor(state, cancel_grace_seconds=0)
    launched = supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:tamper")
    run_store = RunStore(state)
    metadata = run_store.read_metadata(attempt.attempt_id)
    pgid = metadata["process_group_id"]
    metadata["process_birth_time"] += 1000
    run_store.write_metadata(attempt.attempt_id, metadata)

    try:
        with pytest.raises(SupervisorError, match="identity"):
            supervisor.cancel_attempt(attempt.attempt_id, idempotency_key="cancel:tampered")
        assert psutil.pid_exists(metadata["pid"])
    finally:
        os.killpg(pgid, signal.SIGKILL)
        supervisor.wait(attempt.attempt_id)
        supervisor.close()


def test_reconcile_marks_unverifiable_legacy_running_metadata_lost_without_signal(tmp_path):
    state = tmp_path / "state"
    run_store = RunStore(state)
    run_dir = run_store.create_run_dir("legacy_running")
    run_store.write_metadata(
        "legacy_running",
        {
            "run_id": "legacy_running",
            "owned_by_sentinel": True,
            "pid": os.getpid(),
            "process_group_id": os.getpgrp(),
            "status": "running",
        },
    )
    supervisor = OwnedSupervisor(state)

    result = supervisor.reconcile()

    assert result["legacy_lost"] == ["legacy_running"]
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["status"] == "lost"
    assert "lacks verifiable" in metadata["reason"]
    supervisor.close()


def test_supervisor_lease_rejects_a_second_live_owner(tmp_path):
    state = tmp_path / "state"
    first = OwnedSupervisor(state)
    second = OwnedSupervisor(state)
    first.start()
    try:
        with pytest.raises(SupervisorAlreadyRunning):
            second.start()
    finally:
        first.close()


def test_supervisor_heartbeat_prevents_stale_takeover_while_idle(tmp_path):
    state = tmp_path / "state"
    first = OwnedSupervisor(state, lease_stale_after_seconds=1)
    second = OwnedSupervisor(state, lease_stale_after_seconds=1)
    first.start()
    try:
        time.sleep(1.25)
        with pytest.raises(SupervisorAlreadyRunning):
            second.start()
    finally:
        first.close()


def test_metadata_updates_are_serialized_and_terminal_state_never_regresses(tmp_path):
    store = RunStore(tmp_path / "state")
    store.create_run_dir("run_concurrent")
    store.write_metadata("run_concurrent", {"run_id": "run_concurrent", "status": "running"})

    def update(index):
        return store.update_metadata("run_concurrent", {f"field_{index}": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(100)))
    metadata = store.update_metadata("run_concurrent", {"status": "cancelled"})
    assert all(metadata[f"field_{index}"] == index for index in range(100))
    stale = store.update_metadata("run_concurrent", {"status": "cancel_requested", "reason": "stale"})
    assert stale["status"] == "cancelled"
    assert "reason" not in stale


def test_launch_setup_failure_after_durable_launching_becomes_failed(tmp_path):
    state, control, attempt = _attempt(tmp_path, "print('never runs')\n")
    RunStore(state).create_run_dir(attempt.attempt_id)
    supervisor = OwnedSupervisor(state)

    try:
        with pytest.raises(SupervisorError, match="launch failed"):
            supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:setup-failure")
        assert control.project().attempts[attempt.attempt_id].execution_state == "failed"
    finally:
        supervisor.close()


def test_reconcile_deduplicates_recovered_monitors_and_skips_managed_attempts(tmp_path):
    state, _control, attempt = _attempt(
        tmp_path,
        "import time\nprint('READY', flush=True)\nwhile True: time.sleep(0.1)\n",
    )
    first = OwnedSupervisor(state, cancel_grace_seconds=0.05)
    first.launch_attempt(attempt.attempt_id, idempotency_key="launch:reconcile")
    assert first.reconcile()["live"] == [attempt.attempt_id]
    assert attempt.attempt_id not in first._recovered_monitors
    first.close()

    recovered = OwnedSupervisor(state, cancel_grace_seconds=0.05)
    try:
        assert recovered.reconcile()["live"] == [attempt.attempt_id]
        first_thread = recovered._recovered_monitors[attempt.attempt_id]
        assert recovered.reconcile()["live"] == [attempt.attempt_id]
        assert recovered._recovered_monitors[attempt.attempt_id] is first_thread
        assert recovered.cancel_attempt(
            attempt.attempt_id,
            idempotency_key="cancel:reconciled",
        ).execution_state == "cancelled"
    finally:
        recovered.close()


def test_recovered_cancel_requested_becomes_lost_when_process_proof_disappears(tmp_path):
    state, control, attempt = _attempt(
        tmp_path,
        "import time\nprint('READY', flush=True)\nwhile True: time.sleep(0.1)\n",
    )
    first = OwnedSupervisor(state)
    launched = first.launch_attempt(attempt.attempt_id, idempotency_key="launch:recovered-cancel")
    current = control.project().attempts[attempt.attempt_id]
    cancel_requested = control.transition_attempt(
        attempt.attempt_id,
        expected_revision=current.revision,
        execution_state="cancel_requested",
        reason="simulated durable cancellation before supervisor restart",
        idempotency_key="cancel:durable-before-restart",
    )
    RunStore(state).update_metadata(
        attempt.attempt_id,
        {
            "status": "cancel_requested",
            "reason": cancel_requested.reason,
        },
    )
    metadata = RunStore(state).read_metadata(attempt.attempt_id)
    first.close()

    recovered = OwnedSupervisor(state)
    try:
        assert recovered.reconcile()["live"] == [attempt.attempt_id]
        os.killpg(int(metadata["process_group_id"]), signal.SIGKILL)
        deadline = time.time() + 4
        projected = control.project().attempts[attempt.attempt_id]
        while projected.execution_state != "lost" and time.time() < deadline:
            time.sleep(0.05)
            projected = control.project().attempts[attempt.attempt_id]
        assert projected.execution_state == "lost"
        assert projected.control_state == "control_failure"
        assert "recovered process identity" in (projected.reason or "")
        assert projected.ended_at is not None
    finally:
        try:
            if psutil.pid_exists(int(metadata["pid"])):
                os.killpg(int(metadata["process_group_id"]), signal.SIGKILL)
        except OSError:
            pass
        recovered.close()


def test_preflight_rejects_workspace_identity_change(tmp_path):
    state, control, attempt = _attempt(tmp_path, "print('no')\n")
    workspace = next(iter(control.project().workspaces.values()))
    Path(workspace.canonical_root).rename(tmp_path / "workspace-moved")
    supervisor = OwnedSupervisor(state)

    try:
        with pytest.raises(SupervisorError, match="unavailable"):
            supervisor.launch_attempt(attempt.attempt_id, idempotency_key="launch:missing-workspace")
    finally:
        supervisor.close()


@pytest.mark.parametrize("control_state", ["awaiting_approval", "policy_hold", "control_failure"])
def test_preflight_refuses_attempts_behind_control_holds(tmp_path, control_state):
    state, control, attempt = _attempt(tmp_path, "print('must not run')\n")
    held = control.transition_attempt(
        attempt.attempt_id,
        expected_revision=attempt.revision,
        control_state=control_state,
        idempotency_key=f"attempt:hold:{control_state}",
        actor_kind="policy",
        actor_id="test-policy",
    )
    supervisor = OwnedSupervisor(state)

    try:
        with pytest.raises(SupervisorError, match=f"{control_state}, not ready"):
            supervisor.launch_attempt(held.attempt_id, idempotency_key=f"launch:held:{control_state}")
        assert control.project().attempts[held.attempt_id].execution_state == "pending"
        assert not RunStore(state).run_dir(held.attempt_id).exists()
    finally:
        supervisor.close()


def test_preflight_requires_consumed_launch_approval_for_workspace_write(tmp_path):
    state, control, held = _attempt(
        tmp_path,
        "print('only after approval')\n",
        mutation_mode="workspace_write",
    )
    # Simulate a legacy or tampered projection that changed only the control
    # label. Supervisor preflight must independently require authority proof.
    forged_ready = control.transition_attempt(
        held.attempt_id,
        expected_revision=held.revision,
        control_state="ready",
        idempotency_key="attempt:unsafe-release",
    )
    supervisor = OwnedSupervisor(state)
    try:
        with pytest.raises(SupervisorError, match="no consumed launch approval"):
            supervisor.preflight(forged_ready.attempt_id)
    finally:
        supervisor.close()

    second = control.create_attempt(
        held.task_id,
        agent_id=held.agent_id,
        workspace_id=held.workspace_id,
        contract_revision=held.contract_revision,
        idempotency_key="attempt:approved",
    )
    approval = control.request_approval(
        second.task_id,
        attempt_id=second.attempt_id,
        kind="workspace_mutation",
        requested_action="launch",
        requested_by="policy",
        expires_at=time.time() + 60,
        idempotency_key="approval:request",
    )
    control.resolve_approval_for_attempt(
        approval.approval_id,
        approve=True,
        decided_by="local-user",
        expected_revision=approval.revision,
        idempotency_key="approval:resolve",
    )
    supervisor = OwnedSupervisor(state)
    try:
        approved_attempt, *_ = supervisor.preflight(second.attempt_id)
        assert approved_attempt.control_state == "ready"
    finally:
        supervisor.close()


def test_closing_never_started_supervisor_does_not_create_control_state(tmp_path):
    state = tmp_path / "state"
    supervisor = OwnedSupervisor(state)

    supervisor.close()

    assert not (state / "control-plane").exists()
