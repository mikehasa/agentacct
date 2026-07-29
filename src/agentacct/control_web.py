"""Sanitized product projection and server-rendered UI for owned execution.

This module deliberately never receives process-control secrets.  The web
surface exposes decision state; the supervisor keeps PIDs, nonces, absolute
paths, argv, and immutable launch manifests private to the local store.
"""

from __future__ import annotations

import html
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

from .control_plane import ControlProjection, contract_requires_launch_approval


CONTROL_WEB_SCHEMA_VERSION = "agent-chronicle.control-web.v1"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _stamp(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _safe_attempt(record: Any, *, created_at: float | None) -> dict[str, Any]:
    return {
        "attempt_id": record.attempt_id,
        "task_id": record.task_id,
        "contract_revision": record.contract_revision,
        "agent_id": record.agent_id,
        "workspace_id": record.workspace_id,
        "execution_state": record.execution_state,
        "outcome_state": record.outcome_state,
        "control_state": record.control_state,
        "created_at": created_at,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "exit_code": record.exit_code,
        "revision": record.revision,
    }


def sanitize_control_projection(
    projection: ControlProjection,
    *,
    observed_tasks: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return the complete product projection without execution authority.

    In particular this omits workspace roots/store paths, argv templates,
    process ids/groups/birth times, executable/cwd, ownership nonce hashes,
    manifest ids, event payloads/hashes, and arbitrary failure strings.
    """

    tasks = [item.to_dict() for item in sorted(projection.tasks.values(), key=lambda row: row.task_id)]
    contracts = [
        {
            "task_id": item.task_id,
            "revision": item.revision,
            "objective": item.objective,
            "workspace_id": item.workspace_id,
            "mutation_mode": str(item.permission_envelope.get("mutation_mode") or "not_recorded"),
            "launch_approval_required": contract_requires_launch_approval(item.permission_envelope),
            "budget_policy_ids": list(item.budget_policy_ids),
            "success_checks": list(item.success_checks),
            "created_at": item.created_at,
        }
        for item in sorted(projection.contracts.values(), key=lambda row: row.task_id)
    ]
    agents = [
        {
            "agent_id": item.agent_id,
            "display_name": item.display_name,
            "adapter": item.adapter,
            "execution_backend": item.execution_backend,
            "capabilities": list(item.capabilities),
            "command_registered": bool(item.argv_template),
            "enabled": item.enabled,
            "revision": item.revision,
            "updated_at": item.updated_at,
        }
        for item in sorted(projection.agents.values(), key=lambda row: row.agent_id)
    ]
    workspaces = [
        {
            "workspace_id": item.workspace_id,
            "enabled": item.enabled,
            "revision": item.revision,
            "updated_at": item.updated_at,
        }
        for item in sorted(projection.workspaces.values(), key=lambda row: row.workspace_id)
    ]
    attempt_created_at = {
        item.target_id: item.occurred_at
        for item in projection.events
        if item.action == "attempt_created"
    }
    attempts = [
        _safe_attempt(item, created_at=attempt_created_at.get(item.attempt_id))
        for item in sorted(
            projection.attempts.values(),
            key=lambda row: _stamp(attempt_created_at.get(row.attempt_id) or row.started_at or row.ended_at),
            reverse=True,
        )
    ]
    approvals = [
        {
            "approval_id": item.approval_id,
            "task_id": item.task_id,
            "attempt_id": item.attempt_id,
            "kind": item.kind,
            "requested_action": item.requested_action,
            "state": item.state,
            "expires_at": item.expires_at,
            "decided_at": item.decided_at,
            "consumed_at": item.consumed_at,
            "revision": item.revision,
        }
        for item in sorted(projection.approvals.values(), key=lambda row: row.expires_at)
    ]
    budgets = [item.to_dict() for item in sorted(projection.budget_policies.values(), key=lambda row: row.policy_id)]
    schedules = [item.to_dict() for item in sorted(projection.schedules.values(), key=lambda row: row.schedule_id)]
    events = [
        {
            "event_id": item.event_id,
            "occurred_at": item.occurred_at,
            "action": item.action,
            "target_type": item.target_type,
            "target_id": item.target_id,
            "prior_state": item.prior_state,
            "next_state": item.next_state,
        }
        for item in sorted(projection.events, key=lambda row: row.occurred_at, reverse=True)[:24]
    ]
    observed = [
        {
            "task_id": str(item.get("task_id") or ""),
            "title": str(item.get("title") or "Observed Task"),
        }
        for item in observed_tasks
        if str(item.get("task_id") or "").startswith("task_")
    ]
    active_execution = {"pending", "launching", "running", "cancel_requested"}
    summary = {
        "task_count": len(tasks),
        "attempt_count": len(attempts),
        "active_attempt_count": sum(row["execution_state"] in active_execution for row in attempts),
        "running_attempt_count": sum(row["execution_state"] in {"launching", "running", "cancel_requested"} for row in attempts),
        "pending_approval_count": sum(row["state"] == "pending" for row in approvals),
    }
    return {
        "schema_version": CONTROL_WEB_SCHEMA_VERSION,
        "summary": summary,
        "tasks": tasks,
        "contracts": contracts,
        "agents": agents,
        "workspaces": workspaces,
        "attempts": attempts,
        "approvals": approvals,
        "budget_policies": budgets,
        "schedules": schedules,
        "events": events,
        "issues": [{"code": item.code, "line_number": item.line_number} for item in projection.issues],
        "observed_tasks": observed,
        "authority_boundary": {
            "owned_execution": "controllable",
            "external_codex": "observed_only",
            "external_claude_code": "observed_only",
            "external_orchestrators": "observed_only",
        },
    }


def _hidden_fields(csrf_token: str, *, expected_revision: int | None = None) -> str:
    revision = (
        f'<input type="hidden" name="expected_revision" value="{int(expected_revision)}">'
        if expected_revision is not None
        else ""
    )
    return (
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf_token)}">'
        f'<input type="hidden" name="idempotency_key" value="web:{secrets.token_hex(20)}">'
        + revision
    )


def _badge(value: Any) -> str:
    text = str(value or "unknown").replace("_", " ")
    normalized = str(value or "unknown").lower()
    if normalized in {"ready", "running", "succeeded", "verified", "approved", "consumed"}:
        css = "status-ready"
    elif normalized in {"failed", "lost", "rejected", "control_failure", "finding"}:
        css = "status-error"
    elif normalized in {"pending", "launching", "cancel_requested", "awaiting_approval", "policy_hold"}:
        css = "status-finding"
    else:
        css = "status-missing"
    return f'<span class="status {css}">{_esc(text.title())}</span>'


def _task_form(payload: Mapping[str, Any], csrf_token: str) -> str:
    agents = [
        row
        for row in payload.get("agents", [])
        if isinstance(row, Mapping)
        and row.get("enabled")
        and row.get("adapter") == "local_argv"
        and row.get("execution_backend") == "subprocess"
    ]
    workspaces = [row for row in payload.get("workspaces", []) if isinstance(row, Mapping) and row.get("enabled")]
    if not agents or not workspaces:
        return """<section class="section control-setup">
          <div class="section-header"><div><h2>Register owned execution</h2><p>agentacct needs one explicit workspace and one fixed argv adapter before it can start anything.</p></div></div>
          <div class="control-command-grid">
            <div><strong>1 · Register this workspace</strong><code>agentacct control register-workspace --store-dir .agent-sentinel/state --root . --workspace-id project</code></div>
            <div><strong>2 · Register a local argv adapter</strong><code>agentacct control register-agent --store-dir .agent-sentinel/state --agent-id local-agent --display-name &quot;Local agent&quot; --argv-json '[&quot;/absolute/command&quot;,&quot;arg&quot;]'</code></div>
          </div>
          <p class="section-note">Registration does not adopt a running Codex, Claude Code, or orchestrator process. It creates a separate agentacct-owned launch definition.</p>
        </section>"""
    task_options = ['<option value="">Create a new planned Task</option>']
    for row in payload.get("observed_tasks", []):
        if isinstance(row, Mapping):
            task_options.append(
                f'<option value="{_esc(row.get("task_id"))}">{_esc(row.get("title") or "Observed Task")}</option>'
            )
    agent_options = "".join(
        f'<option value="{_esc(row.get("agent_id"))}">{_esc(row.get("display_name") or row.get("agent_id"))}</option>'
        for row in agents
    )
    workspace_options = "".join(
        f'<option value="{_esc(row.get("workspace_id"))}">{_esc(row.get("workspace_id"))}</option>'
        for row in workspaces
    )
    budget_options = '<option value="">No agentacct budget policy</option>' + "".join(
        f'<option value="{_esc(row.get("policy_id"))}">{_esc(row.get("policy_id"))} · {_esc(row.get("action"))}</option>'
        for row in payload.get("budget_policies", [])
        if isinstance(row, Mapping) and row.get("enabled")
    )
    return f"""<section class="section control-create">
      <div class="section-header"><div><h2>Plan an owned attempt</h2><p>Create a reviewable contract first. Nothing starts until you press Launch.</p></div></div>
      <form class="control-task-form" method="post" action="/control/tasks">
        {_hidden_fields(csrf_token)}
        <label><span>Task</span><select name="task_id">{''.join(task_options)}</select><small>Choose an observed Task only when you explicitly want its next attempt governed here.</small></label>
        <label class="control-wide"><span>Objective</span><input name="objective" maxlength="4000" required placeholder="What should this attempt accomplish?"></label>
        <label><span>Workspace</span><select name="workspace_id" required>{workspace_options}</select></label>
        <label><span>Agent adapter</span><select name="agent_id" required>{agent_options}</select></label>
        <label><span>Mutation boundary</span><select name="mutation_mode"><option value="read_only">Read only</option><option value="workspace_write">Workspace write · approval required</option></select></label>
        <label><span>Budget policy</span><select name="budget_policy_id">{budget_options}</select></label>
        <label class="control-wide"><span>Success checks</span><textarea name="success_checks" maxlength="4000" placeholder="One objective check per line"></textarea></label>
        <div class="control-wide control-form-action"><p>agentacct freezes this contract and local launch identity. Agent-native permissions still apply.</p><button class="button" type="submit">Create pending attempt</button></div>
      </form>
    </section>"""


def _attempts(payload: Mapping[str, Any], csrf_token: str) -> str:
    contracts = {
        str(row.get("task_id")): row
        for row in payload.get("contracts", [])
        if isinstance(row, Mapping)
    }
    agents = {
        str(row.get("agent_id")): row
        for row in payload.get("agents", [])
        if isinstance(row, Mapping)
    }
    approvals_by_attempt: dict[str, list[Mapping[str, Any]]] = {}
    for approval in payload.get("approvals", []):
        if not isinstance(approval, Mapping):
            continue
        approval_attempt_id = str(approval.get("attempt_id") or "")
        if approval_attempt_id:
            approvals_by_attempt.setdefault(approval_attempt_id, []).append(approval)
    cards: list[str] = []
    for row in payload.get("attempts", []):
        if not isinstance(row, Mapping):
            continue
        attempt_id = str(row.get("attempt_id") or "")
        execution = str(row.get("execution_state") or "unknown")
        control = str(row.get("control_state") or "unknown")
        contract = contracts.get(str(row.get("task_id") or ""), {})
        agent = agents.get(str(row.get("agent_id") or ""), {})
        controls = ""
        if execution == "pending" and control == "ready":
            controls = f'<form method="post" action="/control/attempts/{_esc(attempt_id)}/launch">{_hidden_fields(csrf_token, expected_revision=int(row.get("revision") or 0))}<button class="button" type="submit">Launch</button></form>'
        elif execution == "pending" and control == "awaiting_approval" and not approvals_by_attempt.get(attempt_id):
            controls = f'<form method="post" action="/control/attempts/{_esc(attempt_id)}/request-approval">{_hidden_fields(csrf_token, expected_revision=int(row.get("revision") or 0))}<button class="button" type="submit">Create approval request</button></form>'
        elif execution in {"running", "cancel_requested"}:
            controls = f'<form method="post" action="/control/attempts/{_esc(attempt_id)}/cancel">{_hidden_fields(csrf_token, expected_revision=int(row.get("revision") or 0))}<button class="button secondary" type="submit">Cancel owned process</button></form>'
        elif execution in {"succeeded", "failed", "cancelled", "lost"}:
            controls = f'<form method="post" action="/control/attempts/{_esc(attempt_id)}/retry">{_hidden_fields(csrf_token, expected_revision=int(row.get("revision") or 0))}<button class="button secondary" type="submit">Retry as new attempt</button></form>'
        cards.append(f"""<article class="control-attempt-card">
          <div class="control-attempt-head"><div><span class="eyebrow">agentacct-owned attempt</span><h3>{_esc(contract.get('objective') or row.get('task_id'))}</h3><p>{_esc(agent.get('display_name') or row.get('agent_id'))} · {_esc(row.get('workspace_id'))}</p></div><a class="see-all" href="/tasks/{_esc(row.get('task_id'))}">Task detail</a></div>
          <div class="control-axis"><div><span>Execution</span>{_badge(execution)}</div><div><span>Outcome</span>{_badge(row.get('outcome_state'))}</div><div><span>Control</span>{_badge(control)}</div></div>
          <div class="control-attempt-action">{controls or '<span class="note">No safe action is currently available.</span>'}</div>
        </article>""")
    if not cards:
        return '<div class="control-empty"><strong>No owned attempts yet</strong><p>Observed sessions stay on Work. Create a contract above when you want agentacct to own the next process.</p></div>'
    return '<div class="control-attempt-grid">' + "".join(cards) + "</div>"


def _approvals(payload: Mapping[str, Any], csrf_token: str) -> str:
    rows: list[str] = []
    for row in payload.get("approvals", []):
        if not isinstance(row, Mapping):
            continue
        actions = ""
        if row.get("state") == "pending":
            base = _hidden_fields(csrf_token, expected_revision=int(row.get("revision") or 0))
            actions = (
                f'<form method="post" action="/control/approvals/{_esc(row.get("approval_id"))}/decision">{base}<input type="hidden" name="decision" value="approve"><button class="button" type="submit">Approve once</button></form>'
                f'<form method="post" action="/control/approvals/{_esc(row.get("approval_id"))}/decision">{_hidden_fields(csrf_token, expected_revision=int(row.get("revision") or 0))}<input type="hidden" name="decision" value="reject"><button class="button secondary" type="submit">Reject</button></form>'
            )
        rows.append(
            f'<li><div><strong>{_esc(str(row.get("requested_action") or "Approval").replace("_", " ").title())}</strong><span>{_esc(row.get("kind"))} · {_esc(row.get("task_id"))}</span></div>{_badge(row.get("state"))}<div class="control-inline-actions">{actions}</div></li>'
        )
    return "".join(rows) or '<li class="control-list-empty">No approval requests.</li>'


def render_control_body(payload: Mapping[str, Any], *, csrf_token: str, notice: str = "", tone: str = "info") -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    supervisor = payload.get("supervisor") if isinstance(payload.get("supervisor"), Mapping) else {}
    notice_html = (
        f'<div class="control-notice {_esc(tone)}" role="status">{_esc(notice)}</div>' if notice else ""
    )
    budgets = "".join(
        f'<li><strong>{_esc(row.get("policy_id"))}</strong><span>{_esc(row.get("metric"))} ≤ {_esc(row.get("limit"))} · {_esc(row.get("basis"))} · {_esc(row.get("action"))}</span>{_badge("ready" if row.get("enabled") else "disabled")}</li>'
        for row in payload.get("budget_policies", [])
        if isinstance(row, Mapping)
    ) or '<li class="control-list-empty">No budget policies registered.</li>'
    schedules = "".join(
        f'<li><strong>{_esc(row.get("schedule_id"))}</strong><span>{_esc(str(row.get("cadence") or "").replace("_", " "))} · {_esc(row.get("task_id"))}</span>{_badge("ready" if row.get("enabled") else "disabled")}</li>'
        for row in payload.get("schedules", [])
        if isinstance(row, Mapping)
    ) or '<li class="control-list-empty">No schedules registered.</li>'
    return f"""
    {notice_html}
    <section class="control-hero">
      <div><div class="eyebrow">Local control plane</div><h2>Govern only what agentacct starts</h2><p>Plan, approve, launch, cancel, and retry agentacct-owned attempts. Existing Codex, Claude Code, Paperclip, and other orchestrator processes remain observed-only.</p></div>
      <div class="control-hero-stats"><div><strong>{_esc(summary.get('active_attempt_count') or 0)}</strong><span>active</span></div><div><strong>{_esc(summary.get('running_attempt_count') or 0)}</strong><span>running</span></div><div><strong>{_esc(summary.get('pending_approval_count') or 0)}</strong><span>approvals</span></div></div>
    </section>
    <div class="control-boundary"><strong>Supervisor · {_esc(str(supervisor.get('state') or 'standby').replace('_', ' ').title())}</strong><span>Signals require agentacct's immutable ownership proof. Detached or external processes are never adopted.</span></div>
    {_task_form(payload, csrf_token)}
    <section class="section"><div class="section-header"><div><h2>Owned execution queue</h2><p>Execution, outcome, and control are independent states.</p></div></div>{_attempts(payload, csrf_token)}</section>
    <div class="control-two-column">
      <section class="section"><div class="section-header"><div><h2>Approvals</h2><p>Expiring and single-use.</p></div></div><ul class="control-list">{_approvals(payload, csrf_token)}</ul></section>
      <section class="section"><div class="section-header"><div><h2>Budget policies</h2><p>Hard action requires authoritative basis.</p></div></div><ul class="control-list">{budgets}</ul></section>
    </div>
    <section class="section"><div class="section-header"><div><h2>Schedules</h2><p>One-shot and fixed local schedules; concurrency is fail-closed.</p></div></div><ul class="control-list">{schedules}</ul></section>
    """


__all__ = ["CONTROL_WEB_SCHEMA_VERSION", "render_control_body", "sanitize_control_projection"]
