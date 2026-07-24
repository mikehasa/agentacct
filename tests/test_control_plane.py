from __future__ import annotations

import json
import hashlib
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_chronicle.control_plane import (
    ControlEvent,
    ControlPlaneError,
    ControlStore,
    IdempotencyConflict,
    InvalidTransition,
    RevisionConflict,
)


def _foundation(tmp_path):
    state = tmp_path / "state"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = ControlStore(state)
    task = store.create_task(origin="planned", idempotency_key="task:create")
    workspace = store.register_workspace(
        workspace_root,
        store_dir=state,
        idempotency_key="workspace:create",
    )
    agent = store.register_agent(
        "agent_test",
        display_name="Test agent",
        adapter="local_argv",
        execution_backend="subprocess",
        argv_template=["/usr/bin/true"],
        capabilities=["local_process"],
        idempotency_key="agent:create",
    )
    contract = store.create_contract(
        task.task_id,
        objective="Prove the local control plane",
        workspace_id=workspace.workspace_id,
        permission_envelope={"mutation_mode": "read_only"},
        success_checks=["exit_code_zero"],
        idempotency_key="contract:create",
    )
    return store, task, workspace, agent, contract


def _approval_foundation(tmp_path):
    state = tmp_path / "state"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = ControlStore(state)
    task = store.create_task(origin="planned", idempotency_key="task:create")
    workspace = store.register_workspace(
        workspace_root,
        store_dir=state,
        idempotency_key="workspace:create",
    )
    agent = store.register_agent(
        "agent_test",
        display_name="Test agent",
        adapter="local_argv",
        execution_backend="subprocess",
        argv_template=["/usr/bin/true"],
        capabilities=["local_process"],
        idempotency_key="agent:create",
    )
    contract = store.create_contract(
        task.task_id,
        objective="Prove approval authority",
        workspace_id=workspace.workspace_id,
        # Deliberately omit launch_approval_required. The mutation mode is
        # independently authoritative and must fail closed.
        permission_envelope={"mutation_mode": "workspace_write"},
        success_checks=["exit_code_zero"],
        idempotency_key="contract:create",
    )
    return store, task, workspace, agent, contract


def test_control_store_is_owner_only_hashed_append_only_and_idempotent(tmp_path):
    store, task, *_ = _foundation(tmp_path)

    retried = store.create_task(origin="planned", idempotency_key="task:create")

    assert retried.task_id == task.task_id
    assert len(store.project().tasks) == 1
    assert len(store.project().events) == 4
    assert stat.S_IMODE(store.control_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.actions_path.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in store.actions_path.read_text().splitlines()]
    assert all(row["record_hash"].startswith("sha256:") for row in rows)

    with pytest.raises(IdempotencyConflict):
        store.create_task(origin="observed", idempotency_key="task:create")


def test_projection_ignores_torn_or_tampered_records_without_state_power(tmp_path):
    store, task, *_ = _foundation(tmp_path)
    valid = store.actions_path.read_bytes()
    row = json.loads(valid.splitlines()[0])
    row["payload"]["record"]["origin"] = "observed"
    store.actions_path.write_bytes(valid + json.dumps(row).encode() + b"\n" + b'{"torn":')

    projection = store.project()

    assert projection.tasks[task.task_id].origin == "planned"
    assert len(projection.issues) == 2
    assert {issue.code for issue in projection.issues} == {"invalid_record"}


def test_projection_rejects_hash_valid_unknown_record_fields(tmp_path):
    store, *_ = _foundation(tmp_path)
    row = json.loads(store.actions_path.read_text().splitlines()[0])
    row["event_id"] = "ctl_" + ("f" * 32)
    row["idempotency_key"] = "task:unsupported-schema"
    row["payload"]["record"]["future_constraint"] = "must-not-be-ignored"
    content = {key: value for key, value in row.items() if key != "record_hash"}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    row["record_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    with store.actions_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")

    projection = store.project()

    assert len(projection.tasks) == 1
    assert projection.issues[-1].code == "invalid_record"
    assert "future_constraint" in projection.issues[-1].message


def test_workspace_identity_cannot_be_rebound_under_existing_id(tmp_path):
    store, _task, workspace, *_ = _foundation(tmp_path)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    event_count = len(store.project().events)

    with pytest.raises(InvalidTransition, match="immutable"):
        store.register_workspace(
            replacement,
            workspace_id=workspace.workspace_id,
            store_dir=store.root,
            enabled=True,
            expected_revision=workspace.revision,
            idempotency_key="workspace:rebind",
        )

    projected = store.project().workspaces[workspace.workspace_id]
    assert projected.canonical_root == workspace.canonical_root
    assert len(store.project().events) == event_count


def test_task_contract_attempt_revisions_and_state_axes_are_strict(tmp_path):
    store, task, workspace, agent, contract = _foundation(tmp_path)
    attempt = store.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:create",
    )

    with pytest.raises(InvalidTransition):
        store.transition_attempt(
            attempt.attempt_id,
            expected_revision=attempt.revision,
            execution_state="running",
            idempotency_key="attempt:skip-launch",
        )
    launching = store.transition_attempt(
        attempt.attempt_id,
        expected_revision=attempt.revision,
        execution_state="launching",
        manifest_id="manifest_test",
        idempotency_key="attempt:launching",
    )
    with pytest.raises(RevisionConflict):
        store.transition_attempt(
            attempt.attempt_id,
            expected_revision=attempt.revision,
            execution_state="failed",
            idempotency_key="attempt:stale-revision",
        )

    running = store.transition_attempt(
        attempt.attempt_id,
        expected_revision=launching.revision,
        execution_state="running",
        pid=123,
        process_group_id=123,
        process_birth_time=100.0,
        process_executable="/usr/bin/true",
        process_cwd=str(tmp_path / "workspace"),
        ownership_nonce_hash="sha256:" + ("1" * 64),
        started_at=101.0,
        idempotency_key="attempt:running",
    )
    with pytest.raises(InvalidTransition, match="cannot change"):
        store.transition_attempt(
            attempt.attempt_id,
            expected_revision=running.revision,
            outcome_state="finding",
            manifest_id="manifest_replaced",
            pid=999,
            idempotency_key="attempt:rewrite-proof",
        )
    finding = store.transition_attempt(
        attempt.attempt_id,
        expected_revision=running.revision,
        outcome_state="finding",
        control_state="awaiting_approval",
        idempotency_key="attempt:finding",
    )
    done = store.transition_attempt(
        attempt.attempt_id,
        expected_revision=finding.revision,
        execution_state="failed",
        ended_at=102.0,
        exit_code=7,
        idempotency_key="attempt:failed",
    )

    assert done.execution_state == "failed"
    assert done.outcome_state == "finding"
    assert done.control_state == "awaiting_approval"
    with pytest.raises(InvalidTransition):
        store.transition_attempt(
            attempt.attempt_id,
            expected_revision=done.revision,
            execution_state="running",
            idempotency_key="attempt:resurrect",
        )


def test_approval_required_attempt_is_born_held_and_cannot_be_overridden(tmp_path):
    store, task, workspace, agent, contract = _approval_foundation(tmp_path)

    attempt = store.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:held",
    )

    assert attempt.control_state == "awaiting_approval"
    with pytest.raises(InvalidTransition, match="must begin awaiting_approval"):
        store.create_attempt(
            task.task_id,
            agent_id=agent.agent_id,
            workspace_id=workspace.workspace_id,
            contract_revision=contract.revision,
            initial_control_state="ready",
            idempotency_key="attempt:unsafe-ready",
        )
    with pytest.raises(ControlPlaneError, match="ready or awaiting_approval"):
        store.create_attempt(
            task.task_id,
            agent_id=agent.agent_id,
            workspace_id=workspace.workspace_id,
            contract_revision=contract.revision,
            initial_control_state="policy_hold",
            idempotency_key="attempt:unsupported-initial",
        )
    store.create_contract(
        task.task_id,
        objective="A newer contract must not erase retry history",
        workspace_id=workspace.workspace_id,
        permission_envelope={"mutation_mode": "workspace_write"},
        expected_revision=contract.revision,
        idempotency_key="contract:advance",
    )
    advanced_agent = store.register_agent(
        agent.agent_id,
        display_name=agent.display_name,
        adapter=agent.adapter,
        execution_backend=agent.execution_backend,
        argv_template=["/usr/bin/false"],
        capabilities=agent.capabilities,
        enabled=True,
        expected_revision=agent.revision,
        idempotency_key="agent:advance",
    )
    retried = store.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:held",
    )
    assert retried.control_state == "awaiting_approval"
    assert retried.agent_revision == agent.revision

    fresh = store.create_attempt(
        task.task_id,
        agent_id=advanced_agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=store.project().contracts[task.task_id].revision,
        idempotency_key="attempt:after-agent-update",
    )
    assert fresh.agent_revision == advanced_agent.revision


def test_replay_rejects_attempt_that_does_not_freeze_current_agent_revision(tmp_path):
    store, task, workspace, agent, contract = _foundation(tmp_path)
    original = store.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:original-agent-revision",
    )
    store.register_agent(
        agent.agent_id,
        display_name=agent.display_name,
        adapter=agent.adapter,
        execution_backend=agent.execution_backend,
        argv_template=["/usr/bin/false"],
        capabilities=agent.capabilities,
        enabled=True,
        expected_revision=agent.revision,
        idempotency_key="agent:update-command",
    )
    forged = original.to_dict()
    forged["attempt_id"] = "attempt_stale_agent"
    event = ControlEvent.create(
        actor_kind="local_user",
        actor_id="local-user",
        action="attempt_created",
        target_type="attempt",
        target_id="attempt_stale_agent",
        expected_revision=0,
        prior_state=None,
        next_state="pending",
        request_id=None,
        causal_parent_id=None,
        idempotency_key="attempt:stale-agent-revision",
        operation_digest="sha256:" + ("c" * 64),
        payload={"record": forged},
    )
    with store.actions_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.to_dict()) + "\n")

    projection = store.project()

    assert original.attempt_id in projection.attempts
    assert "attempt_stale_agent" not in projection.attempts
    assert projection.issues[-1].code == "invalid_record"
    assert "current agent revision" in projection.issues[-1].message


def test_replay_rejects_ready_attempt_for_approval_required_contract(tmp_path):
    store, task, workspace, agent, contract = _approval_foundation(tmp_path)
    held = store.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:legitimate",
    )
    forged = held.to_dict()
    forged.update({"attempt_id": "attempt_forged", "control_state": "ready"})
    event = ControlEvent.create(
        actor_kind="local_user",
        actor_id="local-user",
        action="attempt_created",
        target_type="attempt",
        target_id="attempt_forged",
        expected_revision=0,
        prior_state=None,
        next_state="pending",
        request_id=None,
        causal_parent_id=None,
        idempotency_key="attempt:forged-ready",
        operation_digest="sha256:" + ("b" * 64),
        payload={"record": forged},
    )
    with store.actions_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.to_dict()) + "\n")

    projection = store.project()

    assert projection.attempts[held.attempt_id].control_state == "awaiting_approval"
    assert "attempt_forged" not in projection.attempts
    assert projection.issues[-1].code == "invalid_record"
    assert "must begin awaiting_approval" in projection.issues[-1].message


def test_task_cannot_merge_into_itself(tmp_path):
    store, task, *_ = _foundation(tmp_path)

    with pytest.raises(InvalidTransition, match="itself"):
        store.merge_task(
            task.task_id,
            task.task_id,
            expected_revision=task.revision,
            idempotency_key="task:self-merge",
        )

    assert store.project().tasks[task.task_id].merged_into_task_id is None


def test_approval_is_expiring_revisioned_and_single_use(tmp_path):
    store, task, *_ = _foundation(tmp_path)
    approval = store.request_approval(
        task.task_id,
        kind="dirty_workspace",
        requested_action="launch",
        requested_by="policy",
        expires_at=time.time() + 60,
        idempotency_key="approval:request",
    )
    with pytest.raises(ControlPlaneError, match="boolean"):
        store.decide_approval(
            approval.approval_id,
            approve="false",  # type: ignore[arg-type]
            decided_by="local-user",
            expected_revision=approval.revision,
            idempotency_key="approval:not-a-boolean",
        )
    approved = store.decide_approval(
        approval.approval_id,
        approve=True,
        decided_by="local-user",
        expected_revision=approval.revision,
        idempotency_key="approval:approve",
    )
    consumed = store.consume_approval(
        approval.approval_id,
        expected_revision=approved.revision,
        idempotency_key="approval:consume",
    )

    assert consumed.state == "consumed"
    assert store.consume_approval(
        approval.approval_id,
        expected_revision=approved.revision,
        idempotency_key="approval:consume",
    ).state == "consumed"
    with pytest.raises(InvalidTransition):
        store.consume_approval(
            approval.approval_id,
            expected_revision=consumed.revision,
            idempotency_key="approval:consume-again",
        )


def test_linked_approval_resolution_updates_approval_and_attempt_in_one_event(tmp_path):
    store, task, workspace, agent, contract = _approval_foundation(tmp_path)
    attempt = store.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:approve",
    )
    approval = store.request_approval(
        task.task_id,
        attempt_id=attempt.attempt_id,
        kind="workspace_mutation",
        requested_action="launch",
        requested_by="policy",
        expires_at=time.time() + 60,
        idempotency_key="approval:request:approve",
    )
    before = len(store.project().events)

    consumed, ready = store.resolve_approval_for_attempt(
        approval.approval_id,
        approve=True,
        decided_by="local-user",
        expected_revision=approval.revision,
        idempotency_key="approval:resolve:approve",
    )

    projection = store.project()
    assert consumed.state == "consumed"
    assert consumed.consumed_at is not None
    assert ready.control_state == "ready"
    assert consumed.revision == approval.revision + 1
    assert ready.revision == attempt.revision + 1
    assert len(projection.events) == before + 1
    assert projection.events[-1].action == "approval_resolved"

    retried_approval, retried_attempt = store.resolve_approval_for_attempt(
        approval.approval_id,
        approve=True,
        decided_by="local-user",
        expected_revision=approval.revision,
        idempotency_key="approval:resolve:approve",
    )
    assert retried_approval == consumed
    assert retried_attempt == ready
    assert len(store.project().events) == before + 1
    with pytest.raises(IdempotencyConflict):
        store.resolve_approval_for_attempt(
            approval.approval_id,
            approve=False,
            decided_by="local-user",
            expected_revision=approval.revision,
            idempotency_key="approval:resolve:approve",
        )

    rejected_attempt = store.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:reject",
    )
    rejection = store.request_approval(
        task.task_id,
        attempt_id=rejected_attempt.attempt_id,
        kind="workspace_mutation",
        requested_action="launch",
        requested_by="policy",
        expires_at=time.time() + 60,
        idempotency_key="approval:request:reject",
    )
    rejected, held = store.resolve_approval_for_attempt(
        rejection.approval_id,
        approve=False,
        decided_by="local-user",
        expected_revision=rejection.revision,
        idempotency_key="approval:resolve:reject",
    )
    assert rejected.state == "rejected"
    assert rejected.consumed_at is None
    assert held.control_state == "policy_hold"


@pytest.mark.parametrize("failure_phase", ["before_append", "after_append"])
def test_linked_approval_resolution_recovers_without_partial_state(
    tmp_path,
    monkeypatch,
    failure_phase,
):
    store, task, workspace, agent, contract = _approval_foundation(tmp_path)
    attempt = store.create_attempt(
        task.task_id,
        agent_id=agent.agent_id,
        workspace_id=workspace.workspace_id,
        contract_revision=contract.revision,
        idempotency_key="attempt:failure",
    )
    approval = store.request_approval(
        task.task_id,
        attempt_id=attempt.attempt_id,
        kind="workspace_mutation",
        requested_action="launch",
        requested_by="policy",
        expires_at=time.time() + 60,
        idempotency_key="approval:request:failure",
    )
    original_append = store._append_unlocked
    before = len(store.project().events)

    def fail_resolution_append(event):
        if event.action != "approval_resolved":
            return original_append(event)
        if failure_phase == "after_append":
            original_append(event)
        raise OSError(f"simulated {failure_phase} failure")

    monkeypatch.setattr(store, "_append_unlocked", fail_resolution_append)
    with pytest.raises(OSError, match=failure_phase):
        store.resolve_approval_for_attempt(
            approval.approval_id,
            approve=True,
            decided_by="local-user",
            expected_revision=approval.revision,
            idempotency_key="approval:resolve:failure",
        )

    reloaded = ControlStore(store.root)
    interrupted = reloaded.project()
    interrupted_pair = (
        interrupted.approvals[approval.approval_id].state,
        interrupted.attempts[attempt.attempt_id].control_state,
    )
    assert interrupted_pair in {
        ("pending", "awaiting_approval"),
        ("consumed", "ready"),
    }
    assert interrupted_pair == (
        ("pending", "awaiting_approval")
        if failure_phase == "before_append"
        else ("consumed", "ready")
    )

    resolved, released = reloaded.resolve_approval_for_attempt(
        approval.approval_id,
        approve=True,
        decided_by="local-user",
        expected_revision=approval.revision,
        idempotency_key="approval:resolve:failure",
    )
    assert resolved.state == "consumed"
    assert released.control_state == "ready"
    assert len(reloaded.project().events) == before + 1


def test_replay_cannot_rewrite_approval_authority(tmp_path):
    store, task, *_ = _foundation(tmp_path)
    approval = store.request_approval(
        task.task_id,
        kind="dirty_workspace",
        requested_action="launch",
        requested_by="policy",
        expires_at=time.time() + 60,
        idempotency_key="approval:identity-request",
    )
    record = approval.to_dict()
    record.update(
        {
            "requested_action": "delete_all",
            "state": "approved",
            "decided_by": "local-user",
            "decided_at": time.time(),
            "revision": approval.revision + 1,
        }
    )
    event = ControlEvent.create(
        actor_kind="local_user",
        actor_id="local-user",
        action="approval_decided",
        target_type="approval",
        target_id=approval.approval_id,
        expected_revision=approval.revision,
        prior_state="pending",
        next_state="approved",
        request_id=None,
        causal_parent_id=None,
        idempotency_key="approval:rewrite-authority",
        operation_digest="sha256:" + ("a" * 64),
        payload={"record": record},
    )
    with store.actions_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.to_dict()) + "\n")

    projection = store.project()

    assert projection.approvals[approval.approval_id].state == "pending"
    assert projection.approvals[approval.approval_id].requested_action == "launch"
    assert projection.issues[-1].code == "invalid_record"


def test_hard_budget_policy_requires_authoritative_or_approved_basis(tmp_path):
    store = ControlStore(tmp_path / "state")

    with pytest.raises(ControlPlaneError):
        store.register_budget_policy(
            "policy_estimated",
            scope="task",
            metric="cost_usd",
            limit=1,
            basis="estimated",
            action="cancel",
            idempotency_key="budget:unsafe",
        )

    policy = store.register_budget_policy(
        "policy_billed",
        scope="task",
        metric="cost_usd",
        limit=1,
        basis="provider_billed",
        action="cancel",
        idempotency_key="budget:safe",
    )
    assert policy.basis == "provider_billed"


def test_replay_enforces_budget_safety_and_event_target_binding(tmp_path):
    store = ControlStore(tmp_path / "state")
    unsafe_budget = ControlEvent.create(
        actor_kind="local_user",
        actor_id="local-user",
        action="budget_registered",
        target_type="budget_policy",
        target_id="policy_unsafe",
        expected_revision=0,
        prior_state=None,
        next_state=None,
        request_id=None,
        causal_parent_id=None,
        idempotency_key="replay:unsafe-budget",
        operation_digest="sha256:" + ("0" * 64),
        payload={
            "record": {
                "policy_id": "policy_unsafe",
                "scope": "task",
                "metric": "cost_usd",
                "limit": 1,
                "basis": "estimated",
                "action": "cancel",
                "enabled": True,
                "revision": 1,
                "updated_at": 1.0,
            }
        },
    )
    mismatched_target = ControlEvent.create(
        actor_kind="local_user",
        actor_id="local-user",
        action="task_created",
        target_type="task",
        target_id="task_" + ("1" * 32),
        expected_revision=0,
        prior_state=None,
        next_state=None,
        request_id=None,
        causal_parent_id=None,
        idempotency_key="replay:mismatched-target",
        operation_digest="sha256:" + ("1" * 64),
        payload={
            "record": {
                "task_id": "task_" + ("2" * 32),
                "origin": "planned",
                "created_at": 1.0,
                "revision": 1,
                "merged_into_task_id": None,
            }
        },
    )
    store.control_root.mkdir(parents=True)
    with store.actions_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(unsafe_budget.to_dict()) + "\n")
        handle.write(json.dumps(mismatched_target.to_dict()) + "\n")

    projection = store.project()

    assert projection.budget_policies == {}
    assert projection.tasks == {}
    assert len(projection.issues) == 2


def test_one_shot_and_fixed_schedules_claim_once_and_coalesce_missed_ticks(tmp_path):
    store, task, *_ = _foundation(tmp_path)
    one_shot = store.register_schedule(
        task.task_id,
        cadence="one_shot",
        next_run_at=100.0,
        idempotency_key="schedule:once",
    )
    fixed = store.register_schedule(
        task.task_id,
        cadence="fixed",
        interval_seconds=10,
        next_run_at=100.0,
        idempotency_key="schedule:fixed",
    )

    assert {schedule.schedule_id for schedule in store.due_schedules(now=135.0)} == {
        one_shot.schedule_id,
        fixed.schedule_id,
    }
    claimed_once = store.claim_schedule(
        one_shot.schedule_id,
        expected_revision=one_shot.revision,
        now=135.0,
        idempotency_key="schedule:once:claim",
    )
    claimed_fixed = store.claim_schedule(
        fixed.schedule_id,
        expected_revision=fixed.revision,
        now=135.0,
        idempotency_key="schedule:fixed:claim",
    )

    assert claimed_once.enabled is False and claimed_once.next_run_at is None
    assert claimed_fixed.enabled is True and claimed_fixed.next_run_at == 140.0
    assert claimed_fixed.claim_count == 1

    second_fixed = store.claim_schedule(
        fixed.schedule_id,
        expected_revision=claimed_fixed.revision,
        now=145.0,
        idempotency_key="schedule:fixed:claim-two",
    )
    retried_first = store.claim_schedule(
        fixed.schedule_id,
        expected_revision=fixed.revision,
        now=999.0,
        idempotency_key="schedule:fixed:claim",
    )
    assert second_fixed.revision == 3 and second_fixed.claim_count == 2
    assert retried_first.revision == 2 and retried_first.claim_count == 1
    with pytest.raises(IdempotencyConflict):
        store.claim_schedule(
            fixed.schedule_id,
            expected_revision=999,
            now=999.0,
            idempotency_key="schedule:fixed:claim",
        )


def test_concurrent_same_idempotency_key_commits_exactly_once(tmp_path):
    store = ControlStore(tmp_path / "state")

    def create_once(_index):
        return ControlStore(store.root).create_task(
            origin="planned",
            idempotency_key="task:concurrent",
        ).task_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create_once, range(2)))

    assert ids[0] == ids[1]
    assert len(store.project().events) == 1
    assert len(store.project().tasks) == 1


def test_manifest_is_immutable_owner_only_and_hash_checked(tmp_path):
    store = ControlStore(tmp_path / "state")
    path = store.write_manifest("manifest_test", {"argv": ["/usr/bin/true"], "env_keys": ["PATH"]})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.read_manifest("manifest_test")["payload"]["argv"] == ["/usr/bin/true"]
    assert store.write_manifest("manifest_test", {"argv": ["/usr/bin/true"], "env_keys": ["PATH"]}) == path
    with pytest.raises(IdempotencyConflict):
        store.write_manifest("manifest_test", {"argv": ["/usr/bin/false"]})

    envelope = json.loads(path.read_text())
    envelope["unhashed_authority"] = {"allow": "everything"}
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ControlPlaneError, match="fields do not match"):
        store.read_manifest("manifest_test")


def test_manifest_embedded_id_must_match_filename(tmp_path):
    store = ControlStore(tmp_path / "state")
    store.write_manifest("manifest_source", {"argv": ["/usr/bin/true"]})
    source = json.loads((store.manifests_root / "manifest_source.json").read_text())
    content = {key: source[key] for key in ("schema_version", "manifest_id", "created_at", "payload")}
    source["record_hash"] = "sha256:" + hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    spoof = store.manifests_root / "manifest_spoof.json"
    spoof.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ControlPlaneError, match="filename"):
        store.read_manifest("manifest_spoof")
