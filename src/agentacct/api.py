
import hashlib
import hmac
import html
import json
import threading
import math
import os
import secrets
import time
# ``field`` is aliased: several helpers below use ``field`` as a loop variable.
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from fastapi import Body, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .activation import ActivationStateStore
from .agent_capabilities import agent_capability_manifest
from .client_usage import (
    ClientUsageDiscoveryResult,
    ClientUsageEvent,
    discover_client_usage_with_diagnostics,
)
from .capture import CaptureContext, DEFAULT_CAPTURE_REGISTRY, render_hook_manifest
from .capture.registry import DEFAULT_MAX_PAYLOAD_BYTES
from .capture_runtime import capture_hook_payload
from .glance import GLANCE_SCHEMA_VERSION, GlanceCache, events_fingerprint
from .plan_cost import V1_PLAN_SCHEMA_VERSION, build_v1_plan_payload
from .v1_sessions import (
    V1_SESSION_DETAIL_SCHEMA_VERSION,
    V1_SESSIONS_SCHEMA_VERSION,
    V1SessionsCache,
    build_v1_session_detail,
    build_v1_sessions_view,
    slice_sessions_payload,
)
# Re-exported for compatibility: these names moved verbatim to usage_view (the
# data-layer split). Rebinding/monkeypatching them on THIS module no longer
# reaches usage_snapshot/plan_cost/the TUI — patch agentacct.usage_view instead.
from .usage_view import (
    DashboardUsageRecord,
    DashboardUsageView,
    _build_usage_view,
    _record_from_client_usage,
    _usage_record_time,
)
from .connectors import ControlSignal, evaluate_control_signal
from .connectors.control import normalize_supporting_evidence_ids, validate_supporting_evidence
from .control_plane import (
    ControlPlaneError,
    ControlProjection,
    ControlStore,
    contract_requires_launch_approval,
)
from .cost import (
    PRICING_CATALOG_PATH_ENV,
    CostLedger,
    pricing_catalog_path_for_store,
    reset_pricing_catalog_cache,
)
from .evidence_store import EVIDENCE_STORE_DIRNAME
from .finding_disposition import (
    FindingDispositionConflict,
    FindingDispositionNotFound,
    disposition_for_event,
    finding_target_digest,
    reduce_finding_dispositions,
)
from .env_compat import read_env_alias
from .localhost_guard import install_localhost_guard
from .ingestion_health import (
    IngestionHealthStore,
    V1_INGESTION_SCHEMA_VERSION,
    importer_build_id,
)
from .join_rules import namespace_join_compatible
from .mechanical_checks import build_mechanical_check_events
from .service import SentinelService
from .session_observations import (
    DEFAULT_MECHANICAL_CONFLICT_GROUP_LIMIT,
    DEFAULT_MECHANICAL_CONFLICT_ROW_LIMIT,
    build_session_observations,
    expand_complete_conflict_groups,
    select_session_projection_envelopes,
)
from .source_discovery import UsageSourceDiscovery, discover_usage_sources
from .storage import METADATA_MAX_BYTES, json_utf8_size, validate_run_id
from .store_resolution import is_recognized_global_store
from .supervisor import OwnedSupervisor, SupervisorError
from .task_continuations import ContinuationTaskStore
from .task_identity import TaskIdentityCodec
from .task_outcome import (
    NON_CHECK_RELEVANT_KINDS,
    evidence_event_key,
    finding_check_key,
    latest_check_events,
)
from .task_projection import build_task_projection
from .receipt import (
    RECEIPT_SCHEMA_VERSION,
    build_receipt,
    build_receipt_summary,
    latest_store_activity,
)
from .usage_cube import (
    KNOWN_USAGE_CLIENTS,
    USAGE_CUBE_DAYS_CHOICES,
    USAGE_CUBE_GRANULARITY_CHOICES,
    USAGE_SUMMARY_SCHEMA_VERSION,
    build_usage_cube,
    days_choice_to_int,
    filter_usage_records,
    models_in_records,
    resolve_granularity,
    usage_bucket_date,
)
from .usage_truth import CODEX_REPLAY_QUARANTINE_STATE
from .work_ledger import WorkLedgerCache, _project_identity, _safe_project_label, build_work_ledger
from .work_events import WORK_EVENT_KINDS, WORK_EVENT_STATUSES, WorkEvent

DASHBOARD_USAGE_LIMIT_SESSIONS = 500
# Recent-activity feed on the overview shows a newest-first slice; the full
# history lives on /sessions.
# The pinned Needs-attention strip shows the newest open findings/blockers; the
# rest stay one click away so the strip can never bury the recent-activity feed.
DASHBOARD_RECEIPT_ATTENTION_LIMIT = 2
MECHANICAL_PROJECTION_LIMIT = 10_000
# Conflict repair is a safety path for timestamp-only retry variants. Keep its
# work globally bounded so a conflict-heavy store cannot turn one Work page
# read into thousands of SQL queries or retain an unbounded number of rows.
MECHANICAL_CONFLICT_GROUP_LIMIT = DEFAULT_MECHANICAL_CONFLICT_GROUP_LIMIT
MECHANICAL_CONFLICT_ROW_LIMIT = DEFAULT_MECHANICAL_CONFLICT_ROW_LIMIT


def _is_documented_global_store(store_dir: Path | str) -> bool:
    """Whether this is one of agentacct's machine-wide store layouts.

    Recognizes the legacy ``-global`` dot family AND the new canonical
    ``$XDG_STATE_HOME/agentacct/state`` location (delegated to the single
    recognize-many source of truth in store_resolution).
    """

    return is_recognized_global_store(store_dir)


def _store_scope_and_label(store_dir: Path | str) -> tuple[str, str | None]:
    """(store_scope, own project label), derived once from the store dir.

    ``store_scope`` is EXPLICIT, never guessed from labels: "project" ONLY
    when the store dir structurally matches the conventional
    `<project>/.agent-sentinel/state` layout; every other layout — the
    documented global store (`$HOME/.agent-sentinel-global/state`), any
    custom `--store-dir` — is "custom". Cross-store "session ran in another
    project" claims are only meaningful for project-scoped stores: a
    custom/global store receives every project's context, so the ledger
    never fires `other_project` there (missing beats wrong applies to the
    chip too).

    The label uses the same `_safe_project_label` rule as every session row
    (incl. the worktree-owner remap). Used only to distinguish 'no MCP
    context in this store' from 'session ran in another project' — never to
    probe other projects' stores.
    """

    path = Path(store_dir).expanduser()
    if path.name == "state" and path.parent.name == ".agent-sentinel":
        return "project", _safe_project_label(str(path.parent.parent))
    return "custom", _safe_project_label(str(path))


@dataclass(frozen=True)
class UsageDiscoveryConfig:
    """Controls whether the localhost dashboard may scan client usage stores."""

    enabled: bool = False
    codex_home: Path | None = None
    claude_home: Path | None = None
    opencode_home: Path | None = None
    hermes_home: Path | None = None
    openclaw_home: Path | None = None
    cursor_home: Path | None = None

    @classmethod
    def real_home(cls) -> "UsageDiscoveryConfig":
        return cls(enabled=True)

    @classmethod
    def from_home(cls, home: Path | str) -> "UsageDiscoveryConfig":
        root = Path(home)
        return cls(
            enabled=True,
            codex_home=root / ".codex",
            claude_home=root / ".claude",
            opencode_home=root / ".local" / "share" / "opencode",
            hermes_home=root / ".hermes",
            openclaw_home=root / ".openclaw",
            cursor_home=root / "Library" / "Application Support" / "Cursor",
        )


class MachineCheckRequest(BaseModel):
    name: str = "check"
    before_exit_code: int | None = None
    after_exit_code: int | None = None
    before_summary: str | None = None
    after_summary: str | None = None


class EventRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=80)
    event_type: str = Field(min_length=1, max_length=80)
    run_id: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=120)
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    estimated_output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    usage_confidence: str | None = Field(default=None, max_length=80)
    cost_confidence: str | None = Field(default=None, max_length=80)
    cost_basis: str | None = Field(default=None, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id")
    @classmethod
    def validate_optional_run_id(cls, value: str | None) -> str | None:
        if value is not None:
            validate_run_id(value)
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Real UTF-8 bytes, measured by the same helper the CLI and MCP lanes
        # use: the escaped encoding billed CJK text 2x, so the same payload was
        # accepted on one surface and rejected on another.
        if json_utf8_size(value) > METADATA_MAX_BYTES:
            raise ValueError(f"metadata must be <= {METADATA_MAX_BYTES} bytes when JSON encoded")
        return value


class WorkEventRecordRequest(BaseModel):
    """Transport-neutral semantic claim accepted over trusted localhost HTTP."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=80)
    event_kind: str = Field(min_length=1, max_length=80)
    status: str = Field(default="unknown", max_length=40)
    occurred_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source_event_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        pattern=r"^[^\r\n\x00]+$",
    )
    run_id: str | None = Field(default=None, max_length=128)
    work_id: str | None = Field(default=None, max_length=240)
    section_id: str | None = Field(default=None, max_length=240)
    title: str | None = Field(default=None, max_length=240)
    objective: str | None = Field(default=None, max_length=1200)
    summary: str | None = Field(default=None, max_length=1200)
    blocker: str | None = Field(default=None, max_length=1200)
    next_step: str | None = Field(default=None, max_length=1200)
    client: str | None = Field(default=None, max_length=80)
    client_session_id: str | None = Field(default=None, max_length=240)
    client_transcript_id: str | None = Field(default=None, max_length=240)
    parent_client_session_id: str | None = Field(default=None, max_length=240)
    turn_id: str | None = Field(default=None, max_length=240)
    message_id: str | None = Field(default=None, max_length=240)
    request_id: str | None = Field(default=None, max_length=240)
    files: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("event_kind")
    @classmethod
    def validate_event_kind(cls, value: str) -> str:
        if value not in WORK_EVENT_KINDS:
            raise ValueError("unsupported Work Event kind")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in WORK_EVENT_STATUSES:
            raise ValueError("unsupported Work Event status")
        return value

    @field_validator("source_event_id")
    @classmethod
    def validate_source_event_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("source_event_id must not be blank")
        return value

    @field_validator("run_id")
    @classmethod
    def validate_work_event_run_id(cls, value: str | None) -> str | None:
        if value is not None:
            validate_run_id(value)
        return value


class ControlSignalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(pattern="^(warn|pause|cancel|block)$")
    target_type: str = Field(default="execution", min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=240)
    recommendation: str = Field(min_length=1, max_length=1200)
    requested_mode: str = Field(default="advisory", pattern="^(advisory|hard)$")
    evidence_basis: str = Field(default="unknown", max_length=80)
    cost_confidence: str = Field(default="unknown", max_length=80)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    explicit_conservative_approval: bool = False
    controller_owns_execution: bool = False
    conflicting: bool = False
    expires_at: str | None = Field(default=None, max_length=80)
    idempotency_key: str | None = Field(default=None, max_length=240)

    @field_validator("supporting_evidence_ids")
    @classmethod
    def validate_supporting_ids(cls, value: list[str]) -> list[str]:
        return list(normalize_supporting_evidence_ids(value))


def _fmt_int(value: Any) -> str:
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        number = 0
    return f"{number:,}"


def _fmt_time(value: Any) -> str:
    try:
        timestamp = float(value)
    except (OverflowError, TypeError, ValueError):
        return ""
    if not math.isfinite(timestamp) or timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        # Client metadata is client-authored: one absurd-but-finite timestamp
        # (ms epoch, year 99999, 1e300) must never 500 the whole dashboard.
        return ""


def _human_client(value: Any) -> str:
    labels = {
        "claude-code": "Claude Code",
        "codex": "Codex",
        "hermes": "Hermes",
        "opencode": "OpenCode",
        "openclaw": "OpenClaw",
        "cursor": "Cursor",
    }
    text = str(value or "").strip()
    return labels.get(text, text.replace("-", " ").title() if text else "Unknown")


def _short_text(value: Any, *, max_length: int = 36) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 1] + "…"


def _usage_record_totals(records: list[DashboardUsageRecord]) -> dict[str, Any]:
    estimated_cost = 0.0
    estimated_count = 0
    unpriced_count = 0
    for record in records:
        if not record.usage_additive:
            continue
        if record.estimated_cost_usd is None:
            unpriced_count += 1
            continue
        estimated_cost += record.estimated_cost_usd
        estimated_count += 1
    main_sessions = sum(1 for record in records if record.session_kind.lower() == "root")
    child_sessions = sum(1 for record in records if record.session_kind.lower() == "child")
    internal_sessions = sum(1 for record in records if record.session_kind.lower() == "internal")
    return {
        "records": len(records),
        "sessions": len(records),
        "additive_records": sum(1 for record in records if record.usage_additive),
        "excluded_non_additive_records": sum(1 for record in records if not record.usage_additive),
        "user_sessions": main_sessions + child_sessions,
        "main_sessions": main_sessions,
        "child_sessions": child_sessions,
        "internal_sessions": internal_sessions,
        "input_tokens": sum(record.input_tokens for record in records),
        "output_tokens": sum(record.output_tokens for record in records),
        "cached_input_tokens": sum(record.cached_input_tokens for record in records),
        "cache_creation_input_tokens": sum(record.cache_creation_input_tokens for record in records),
        "cache_read_input_tokens": sum(record.cache_read_input_tokens for record in records),
        "reasoning_output_tokens": sum(record.reasoning_output_tokens for record in records),
        "total_tokens": sum(record.total_tokens for record in records),
        "total_tokens_including_cached": sum(record.total_tokens_including_cached for record in records),
        "client_reported_cost_usd": sum(float(record.client_reported_cost_usd or 0.0) for record in records),
        "estimated_equivalent_cost_usd": estimated_cost,
        "estimated_equivalent_cost_sessions": estimated_count,
        "unpriced_sessions": unpriced_count,
    }


def _usage_exclusion_reason(records: Iterable[DashboardUsageRecord]) -> str:
    reasons = {
        record.usage_normalization_state
        for record in records
        if record.usage_normalization_state
    }
    if len(reasons) == 1:
        return next(iter(reasons))
    if len(reasons) > 1:
        return "mixed_source_identity_or_lineage_normalization"
    # Keep the stable v1 empty-state value for callers that compare the full
    # response shape; no excluded row means this reason is informational only.
    return CODEX_REPLAY_QUARANTINE_STATE


def _usage_event_totals(events: list[ClientUsageEvent]) -> dict[str, Any]:
    return _usage_record_totals([_record_from_client_usage(event) for event in events])


def _discover_local_usage(
    config: UsageDiscoveryConfig,
    *,
    limit_sessions: int = DASHBOARD_USAGE_LIMIT_SESSIONS,
    include_diagnostics: bool = False,
    client: str = "all",
) -> list[ClientUsageEvent] | ClientUsageDiscoveryResult:
    if not config.enabled:
        empty = ClientUsageDiscoveryResult(events=[], diagnostics={})
        return empty if include_diagnostics else []
    result = discover_client_usage_with_diagnostics(
        client=client,
        limit_sessions=limit_sessions,
        codex_home=config.codex_home,
        claude_home=config.claude_home,
        opencode_home=config.opencode_home,
        hermes_home=config.hermes_home,
        openclaw_home=config.openclaw_home,
        cursor_home=config.cursor_home,
    )
    return result if include_diagnostics else result.events


def _discover_local_usage_sources(config: UsageDiscoveryConfig) -> list[UsageSourceDiscovery]:
    if not config.enabled:
        return []
    return discover_usage_sources(
        codex_home=config.codex_home,
        claude_home=config.claude_home,
        opencode_home=config.opencode_home,
        hermes_home=config.hermes_home,
        openclaw_home=config.openclaw_home,
        cursor_home=config.cursor_home,
    )


# ---------------------------------------------------------------------------
# /api/control sanitizer — moved verbatim from the retired control_web module
# (this is the JSON projection sanitizer, not display code: it strips
# execution authority — paths, argv, pids, nonces — from the product view).
# ---------------------------------------------------------------------------

CONTROL_WEB_SCHEMA_VERSION = "agent-chronicle.control-web.v1"


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


def _dashboard_importer_version() -> str:
    return importer_build_id()


# A dashboard refresh used to redirect to ``/?imported=…&scanned=…&…`` — an 18-
# field query string that cluttered the address bar on every refresh. The
# summary now rides a one-time cookie instead, so the URL stays a clean ``/`` and
# the post-refresh banner still shows once (then the cookie is cleared on read).
# --- Live refresh progress (background thread + no-JS /refreshing page) -------
# Rough weights so the overall bar advances sensibly across a scan; claude-code
# is the slow one, and the evidence reconcile is a big count-less tail. The
# scanning band tops out near 0.60, reconcile carries it to ~0.92, done = 1.0.
# A refresh still "running" after this long is treated as dead so a new one can
# start (the daemon thread may have died without reporting).
# Plain-language names for the frozen refusal reason vocabulary. Anything the
# vocabulary gains without a label here still renders (as its code), so a new
# reason can never be silently dropped from the table.
# Confidence normalization moved to usage_cube (Phase 3.5b): ONE rule shared
# by the confidence tables here and the cube's dominant-confidence buckets.
# ---------------------------------------------------------------------------
# Phase 2 Batch B: Product-tab session rollup / work items / attention /
# reconciliation fragments. Hard display rules: session ids render ONLY via
# the ledger's precomputed client_session_id_short labels (single source of
# truncation — never re-derived here), project labels are the ledger's
# pre-redacted last path segments, esc() wraps every interpolation, and
# total tokens and estimated cost are the headline while component figures
# stay labeled. Nothing in these fragments allocates usage the canonical ledger
# did not allocate.
# ---------------------------------------------------------------------------

SESSION_ROLLUP_DISPLAY_LIMIT = 40
def _session_short_labels(rollup_sessions: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(client, full session id) -> the ledger's collision-safe short label."""

    labels: dict[tuple[str, str], str] = {}
    for entry in rollup_sessions:
        if not isinstance(entry, dict):
            continue
        client = str(entry.get("client") or "")
        session_id = str(entry.get("client_session_id") or "")
        short = str(entry.get("client_session_id_short") or "")
        if client and session_id and short:
            labels[(client, session_id)] = short
    return labels


def _top_level_session_keys(rollup_sessions: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Which rollup entries render as their own top-level session rows.

    Nesting is exactly ONE level: an entry renders as a top-level row UNLESS
    its direct parent is itself rendered as a top-level row (then it appears
    as that parent's labeled children line). Consequences, all deliberate:

    - depth-2 lineage: the grandchild's parent is nested (not rendered), so
      the grandchild renders top-level with a "child of <parent>" chip;
    - parent-pointer cycles are broken deterministically: every cycle member
      renders top-level (corrupt/hostile lineage data must hide nothing);
    - a parent absent from the rollup means the child renders top-level.

    INVARIANT (regression-tested): every rollup entry either renders as its
    own top-level row or is a direct child of exactly one rendered top-level
    row — nothing vanishes, nothing double-counts.
    """

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    parent_of: dict[tuple[str, str], tuple[str, str] | None] = {}
    order: list[tuple[str, str]] = []
    for entry in rollup_sessions:
        if not isinstance(entry, dict):
            continue
        key = (str(entry.get("client") or ""), str(entry.get("client_session_id") or ""))
        if key in by_key:
            continue
        by_key[key] = entry
        order.append(key)
        parent = (entry.get("related") or {}).get("parent")
        if isinstance(parent, dict) and parent.get("client_session_id") is not None:
            parent_of[key] = (key[0], str(parent.get("client_session_id")))
        else:
            parent_of[key] = None

    top_level: dict[tuple[str, str], bool] = {}
    for start in order:
        if start in top_level:
            continue
        # Walk the parent chain until a decided node, a terminal node, or a
        # cycle; then unwind: a node is top-level exactly when its parent is
        # not rendered top-level.
        path: list[tuple[str, str]] = []
        position: dict[tuple[str, str], int] = {}
        current = start
        while True:
            if current in top_level:
                parent_is_top = top_level[current]
                break
            if current in position:
                # Parent-pointer cycle: every member renders top-level.
                for member in path[position[current]:]:
                    top_level[member] = True
                path = path[: position[current]]
                parent_is_top = True
                break
            position[current] = len(path)
            path.append(current)
            parent = parent_of.get(current)
            if parent is None or parent == current or parent not in by_key:
                # No parent, self-parent, or parent absent from the rollup:
                # this node renders top-level.
                top_level[current] = True
                path.pop()
                parent_is_top = True
                break
            current = parent
        for node in reversed(path):
            flag = not parent_is_top
            top_level[node] = flag
            parent_is_top = flag
    return {key for key, flag in top_level.items() if flag}


def _session_display_sort_key(entry: dict[str, Any]) -> Any:
    """The ONE session-list display order (tab-era rule, unchanged):
    attributed sessions first, then sessions with recorded work, then most
    recent activity. Shared by the grouped/roots populations built in
    _dashboard_page_data and the /sessions all-flat view."""

    return (
        0 if (entry.get("join") or {}).get("state") == "attributed" else 1,
        0 if int(((entry.get("work") or {}).get("counts") or {}).get("total") or 0) > 0 else 1,
        -float(entry.get("last_activity_at") or 0.0),
    )


# Grouped-view collapsing rules per usage-bearing event kind: proxy and
# diagnostic rows group exactly like import rows (unbounded row-level
# passthrough is how sections got drowned), just labeled distinctly.
# ---------------------------------------------------------------------------
# Phase 3.5a — multi-page dashboard foundation.
#
# Every server-rendered page shares ONE layout helper (`_page_doc`). The
# primary product navigation is Work / Control / Usage / Advanced; stable Sessions,
# Raw, and Evidence-v2 routes inherit the active state of their owning area.
# `/?tab=raw` and `/?tab=product` still 302 to their compatible homes.
# Performance contract: saved-data and evidence pages never run the live
# client-log scan; only Refresh & save and /raw perform discovery.
# ---------------------------------------------------------------------------

# Primary top nav: the product surface only. Control (governs agentacct-owned
# launched processes) and Advanced (forensic evidence tools) are demoted to a
# subtle footer entry — their routes stay fully reachable, they just no longer
# clutter the primary nav for the common observe-only flow.
# Secondary surfaces, reachable via a demoted footer link (not the primary nav).
# Product navigation names user goals, not internal projections.  Legacy and
# forensic routes remain stable, but highlight the product area that owns
# them instead of becoming eight peer destinations in the primary nav.
# PRD §5.1 / §11.3 (locked): the cost-basis disclosure that travels with the
# Est. cost co-headline on the Overview and the /tokens page header.
# Design tokens (PRD §7): spacing/type scale, status colors lifted verbatim
# from the existing chips, and the 6-color platform lane palette (5 known
# clients + other) shared by badges/bars and the Phase 3.5b SVG chart. Light
# theme only — dark mode is deferred past Phase 3.5 (locked decision Q4).
def _store_display_label(store_dir: Path | str, store_scope: str, store_project_label: str | None) -> str:
    """Redacted store label for the identity strip: leaf segments only.

    Project-scoped stores show their project label; every other layout shows
    the store directory's own leaf (its parent for the conventional
    ``.../state`` leaf, which names nothing). Never an absolute path.
    """

    if store_scope == "project" and store_project_label:
        return store_project_label
    if _is_documented_global_store(store_dir):
        return "All projects"
    path = Path(store_dir).expanduser()
    if path.name == "state":
        label = _safe_project_label(str(path.parent))
        if label:
            return label
    return _safe_project_label(str(path)) or "local store"


_ADVANCED_INGESTION_RECOVERY_CODES = frozenset(
    {
        "source_home_selection_required",
        "source_adapter_incompatible",
        "source_identity_unresolved",
        "source_path_unsafe",
        "source_snapshot_required",
        "source_changed_during_scan",
        "source_read_permission_required",
        "source_namespace_conflict",
        "same_watermark_conflict",
        "source_watermark_unorderable",
        "invalid_observation",
    }
)


@dataclass
class _DashboardPageData:
    """Everything the page renderers consume, computed from SAVED data only.

    ``usage_view.local_records`` is non-empty only when the caller (the /raw
    route) passed a live preview scan; product pages never pass one, which is
    what keeps the live client-log scan off their path (PRD §8).
    """

    events: list[dict[str, Any]]
    cost_events: list[dict[str, Any]]
    usage_view: DashboardUsageView
    ledger: dict[str, Any]
    ledger_overview: dict[str, Any]
    rollup_sessions: list[dict[str, Any]]
    rollup_summary: dict[str, Any]
    rollup_totals: dict[str, Any]
    session_short_labels: dict[tuple[str, str], str]
    top_level_sessions: list[dict[str, Any]]
    work_items: list[dict[str, Any]]
    usage_reconciliation: list[dict[str, Any]]
    attention_groups: list[dict[str, Any]]
    attention_total_items: int
    ledger_insights: dict[str, Any]
    store_label: str
    store_scope: str
    store_project_identity: str | None
    continuation_projection: dict[str, Any]
    task_csrf_token: str
    ingestion_health: dict[str, Any]
    mechanical_check_events: list[dict[str, Any]]
    task_identity: TaskIdentityCodec | None = None



def _dashboard_page_data(
    *,
    events: list[dict[str, Any]],
    cost_events: list[dict[str, Any]],
    run_reports: list[dict[str, Any]],
    session_observations: list[dict[str, Any]] | None = None,
    session_observation_diagnostics: dict[str, int] | None = None,
    mechanical_check_events: list[dict[str, Any]] | None = None,
    store_project_label: str | None,
    store_project_identity: str | None = None,
    store_scope: str | None,
    store_label: str,
    local_usage_preview: list[ClientUsageEvent] | None = None,
    continuation_projection: dict[str, Any] | None = None,
    task_csrf_token: str = "",
    ingestion_health: dict[str, Any] | None = None,
    task_identity: TaskIdentityCodec | None = None,
    ledger: dict[str, Any] | None = None,
) -> _DashboardPageData:
    usage_view = _build_usage_view(list(local_usage_preview or []), events)
    # Built before any table renders: the rollup's collision-safe short
    # session labels are the ONE session-label source for every cell —
    # product pages AND raw page (full ids are href-only, locked decision).
    # A pre-built ``ledger`` may be injected by a caller that already holds
    # one for THIS event set (the /v1 task lane passes the shared, cached
    # derived ledger the sessions lane keeps warm) — same builder, same
    # inputs, so the projection is identical while the multi-second reduce is
    # paid once and shared. run_reports/cost_events/session_observations are
    # then unused for the ledger; they still populate the page-data fields.
    if ledger is None:
        ledger = build_work_ledger(
            events,
            run_reports=run_reports,
            cost_events=cost_events,
            session_observations=session_observations,
            session_observation_diagnostics=session_observation_diagnostics,
            store_project_label=store_project_label,
            store_scope=store_scope,
        )
    # Batch A contract: session_rollup and attention_groups are consumed
    # VERBATIM — grouping/labeling is never reimplemented in api.py.
    session_rollup = ledger.get("session_rollup") if isinstance(ledger.get("session_rollup"), dict) else {}
    rollup_sessions = session_rollup.get("sessions") if isinstance(session_rollup.get("sessions"), list) else []
    rollup_summary = session_rollup.get("summary") if isinstance(session_rollup.get("summary"), dict) else {}
    rollup_totals = rollup_summary.get("totals") if isinstance(rollup_summary.get("totals"), dict) else {}
    attention_payload = ledger.get("attention_groups") if isinstance(ledger.get("attention_groups"), dict) else {}
    attention_groups = attention_payload.get("groups") if isinstance(attention_payload.get("groups"), list) else []
    # Session rows nest exactly one level (see _top_level_session_keys).
    # Display order (unchanged from the tab era): attributed sessions first,
    # then sessions with recorded work, then most recent activity.
    top_level_keys = _top_level_session_keys(rollup_sessions)
    top_level_sessions = sorted(
        (
            entry
            for entry in rollup_sessions
            if isinstance(entry, dict)
            and (str(entry.get("client") or ""), str(entry.get("client_session_id") or "")) in top_level_keys
        ),
        key=_session_display_sort_key,
    )
    return _DashboardPageData(
        events=events,
        cost_events=cost_events,
        usage_view=usage_view,
        ledger=ledger,
        ledger_overview=ledger["overview"],
        rollup_sessions=rollup_sessions,
        rollup_summary=rollup_summary,
        rollup_totals=rollup_totals,
        session_short_labels=_session_short_labels(rollup_sessions),
        top_level_sessions=top_level_sessions,
        work_items=ledger["work_items"],
        usage_reconciliation=ledger.get("usage_reconciliation") if isinstance(ledger.get("usage_reconciliation"), list) else [],
        attention_groups=attention_groups,
        attention_total_items=int(attention_payload.get("total_items") or 0),
        ledger_insights=ledger.get("insights") if isinstance(ledger.get("insights"), dict) else {},
        store_label=store_label,
        store_scope=store_scope or "custom",
        store_project_identity=store_project_identity,
        continuation_projection=dict(continuation_projection or {}),
        task_csrf_token=task_csrf_token,
        ingestion_health=dict(ingestion_health or {}),
        mechanical_check_events=list(mechanical_check_events or []),
        task_identity=task_identity,
    )


# ---------------------------------------------------------------------------
# Phase 3.5b — the Tokens explorer (PRD §5). All fragments render the usage
# cube (usage_cube.build_usage_cube) over SAVED rows; the honesty rules are
# marks, not captions: total token volume including caches leads every table
# and bar, estimated cost remains adjacent, and uncached input/output plus
# cache creation/read stay visible as the component breakdown.  The stable
# ``fresh_tokens`` JSON field remains available for efficiency analysis but
# is no longer promoted as the product headline.
# ---------------------------------------------------------------------------

# Left gutter sized so a right-aligned 11px axis label up to ~100M-token /
# ~10B-cache-read scale ("500,000,000" ≈ 68 viewBox units at the system-ui
# digit advance) never clips the viewBox left edge.
# One absurdly wide range (days=all + granularity=daily override) must not
# emit thousands of rects; the capped By period table below the chart is the
# fallback (both caps are stated in the chart's cap note).
# The By period table's own cap — named in the chart cap note, so the two
# notes can never disagree about what is shown.
# ---------------------------------------------------------------------------
# Overview usage charts (Dashboard v2, Stage 2). Server-rendered inline SVG,
# zero JS (the dashboard CSP forbids script). Hover detail is native SVG
# <title>; the daily breakdown selector is a query-param link that re-renders
# the whole page. Honesty as marks: bar/line height is TOTAL reported tokens
# including cache; held (non-additive) usage rows are disclosed in words, never
# summed into the picture and never drawn as a fabricated zero.
# ---------------------------------------------------------------------------

# The bar chart carries a y-axis, so it needs a left gutter wide enough for a
# compact label ("24.4B"); the line chart has no axis labels and spans full
# width from _OV_CHART_LEFT.
# Model / agent-model lanes keep their owning agent's hue; sub-models within one
# client step opacity so two Claude models are still distinguishable without
# inventing a fresh color per model (matches the approved preview).
# Cap the stacked-breakdown series so the legend and stack stay legible; the
# tail folds into one honestly labeled "Other models" lane rather than being
# silently dropped.
# Data-driven filter pill rows (models on /tokens, projects on /sessions)
# are capped like every other data-driven list — a store accumulating
# distinct values over years must not turn the filter controls themselves
# into the page-bloat vector.
def _usage_history_outside_range(
    *,
    current_cube: Mapping[str, Any],
    all_time_cube: Mapping[str, Any],
    records: list[DashboardUsageRecord],
    model: str | None,
    days: int | None,
    today: date,
) -> list[dict[str, Any]]:
    """Clients absent solely because every matching row is outside the range.

    Both cubes must already carry the same client/model filters. Unknown-time
    rows deliberately suppress the claim: a bounded range excludes them, but
    agentacct cannot honestly say which side of the range they belong on.
    The raw epoch is returned so JSON consumers are not coupled to server-local
    display formatting.
    """

    # The helper is meaningful only for a bounded range. Keep the guard here
    # as well as at the callers so a future direct caller cannot describe
    # all-time or malformed input as "older history".
    if days is None or days < 1:
        return []
    range_start = today - timedelta(days=days - 1)
    current_clients = {
        str(row.get("client") or "")
        for row in current_cube.get("by_client", [])
        if isinstance(row, Mapping) and int(row.get("rows") or 0)
    }
    result: list[dict[str, Any]] = []
    for row in all_time_cube.get("by_client", []):
        if not isinstance(row, Mapping):
            continue
        client_name = str(row.get("client") or "")
        rows = int(row.get("rows") or 0)
        if not client_name or not rows or client_name in current_clients:
            continue
        matching_records = [
            record
            for record in records
            if record.client == client_name and (model is None or record.model == model)
        ]
        timestamps = [_usage_record_time(record) for record in matching_records]
        if len(matching_records) != rows or not timestamps:
            continue
        # A bad/missing timestamp is not evidence of old history. Fail closed
        # instead of describing an unknown-time row as outside the window.
        local_days = [usage_bucket_date(timestamp) for timestamp in timestamps]
        if any(day is None for day in local_days):
            continue
        # Absence from a bounded cube can mean "older than the window" OR
        # "dated in the future". Only the former is preserved-history
        # evidence. A mixed old/future population also fails closed.
        if any(day >= range_start for day in local_days if day is not None):
            continue
        result.append(
            {
                "client": client_name,
                "rows": rows,
                "sessions": int(row.get("sessions") or 0),
                "latest_activity_at": max(timestamps),
            }
        )
    return sorted(result, key=lambda entry: entry["client"])


def _work_item_display_title(item: dict[str, Any]) -> str:
    """Return a user-facing work title without promoting an internal id."""

    title = str(item.get("title") or "").strip()
    internal_values = {
        str(item.get("work_id") or "").strip(),
        str(item.get("section_id") or "").strip(),
    }
    if title and title not in internal_values:
        return title
    # No explicit title: prefer the agent's own summary, then a meaningful kind.
    # Never route ``kind`` through the usage-counter label helper — its
    # "unknown" -> "Not reported" mapping is for missing NUMBERS and reads as a
    # self-contradiction next to the "Agent reported" tag on a step the agent
    # did record.
    summary = str(item.get("summary") or "").strip()
    if summary:
        return _short_text(summary, max_length=60)
    kind = str(item.get("kind") or item.get("phase") or "").strip()
    if kind and kind != "unknown":
        return f"{kind.replace('_', ' ').title()} step"
    return f"Untitled step · {_human_client(item.get('client'))}"


def _overview_project_in_scope(
    data: _DashboardPageData,
    project: Any,
    project_identity: Any = None,
    project_identity_state: Any = None,
) -> bool:
    if data.store_scope != "project":
        return True
    if str(project_identity_state or "").strip() == "conflicting":
        return False
    identity = str(project_identity or "").strip()
    if identity and data.store_project_identity:
        return identity == data.store_project_identity
    project_label = str(project or "").strip()
    # Events written directly into a project store may predate project_dir
    # capture. Keep missing identities/labels for legacy compatibility, but an
    # explicit full-path identity always wins over the friendly basename: two
    # unrelated repositories are allowed to share the same leaf directory.
    return not project_label or project_label.casefold() == data.store_label.casefold()


def _session_identity_key(entry: Mapping[str, Any]) -> tuple[str, str]:
    return (str(entry.get("client") or ""), str(entry.get("client_session_id") or ""))


def _evidence_event_key(event: Mapping[str, Any]) -> tuple[str, ...]:
    return evidence_event_key(event)


def _task_title(task: Mapping[str, Any]) -> str:
    override = str(task.get("title_override") or "").strip()
    if override:
        return override
    primary = task.get("primary_root") if isinstance(task.get("primary_root"), Mapping) else {}
    primary_key = (str(primary.get("client") or ""), str(primary.get("client_session_id") or ""))
    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    root_session = next(
        (
            session
            for session in sessions
            if isinstance(session, Mapping) and _session_identity_key(session) == primary_key
        ),
        None,
    )
    # Work steps explain what happened inside a chat; they must not rename the
    # chat itself. The ledger only exposes client_session_title after the
    # trusted explicit-title gate, so this preserves the privacy boundary while
    # matching the name the user sees in the coding client sidebar.
    if isinstance(root_session, Mapping):
        client_title = str(root_session.get("client_session_title") or "").strip()
        if client_title:
            return _short_text(client_title, max_length=160)

    items = [
        item
        for item in (task.get("work_items") if isinstance(task.get("work_items"), list) else [])
        if isinstance(item, Mapping)
    ]

    def ordered(candidates: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return sorted(
            candidates,
            key=lambda item: (
                float(item.get("started_at") or item.get("updated_at") or 0.0),
                str(item.get("section_id") or item.get("work_id") or ""),
            ),
        )

    preferred_sets = [
        [
            item
            for item in items
            if str(item.get("kind") or "") != "review"
            and (
                str(item.get("client") or item.get("reporting_source") or ""),
                str(item.get("client_session_id") or ""),
            )
            == primary_key
        ],
        [item for item in items if str(item.get("kind") or "") != "review"],
        items,
    ]
    for candidates in preferred_sets:
        if candidates:
            return _work_item_display_title(dict(ordered(candidates)[0]))
    primary_session = next(
        (
            session
            for session in sessions
            if isinstance(session, Mapping)
            and _session_identity_key(session)
            == (str(primary.get("client") or ""), str(primary.get("client_session_id") or ""))
        ),
        {},
    )
    project = str(primary_session.get("project") or "").strip() if isinstance(primary_session, Mapping) else ""
    client_label = _human_client(primary.get("client"))
    return f"{client_label} in {project}" if project else f"Untitled {client_label} chat"


def _receipt_attention_priority(summary: Mapping[str, Any]) -> int | None:
    """Dashboard review priority for one compact Receipt summary.

    Open machine findings and recorded failures come before agent-reported
    blockers. Human-resolved or superseded findings retain their failed-check
    history without returning to the attention queue.
    """

    decision = summary.get("decision_status")
    evidence = summary.get("evidence_strength")
    decision_key = str(
        decision.get("key") if isinstance(decision, Mapping) else ""
    ).strip()
    failed_checks = (
        int(evidence.get("checks_failed") or 0)
        if isinstance(evidence, Mapping)
        else 0
    )
    settled_finding_keys = {"finding_superseded", "finding_resolved_by_user"}
    has_finding = (
        failed_checks > 0 and decision_key not in settled_finding_keys
    ) or decision_key in {"finding", "failed"}
    if has_finding:
        return 0
    if decision_key == "blocked":
        return 1
    return None


def _dashboard_receipt_attention(
    tasks: Sequence[Mapping[str, Any]],
    *,
    latest_store_activity_at: float | None,
) -> dict[str, Any]:
    """Exact all-store attention count plus a bounded Dashboard preview.

    ``tasks`` is newest-first. Retaining at most two rows per priority class
    keeps memory bounded while the full scan proves whether the queue is empty.
    """

    preview_by_priority: tuple[list[dict[str, Any]], list[dict[str, Any]]] = ([], [])
    total = 0
    for task in tasks:
        row = build_receipt_summary(
            task,
            public_task_id=str(task.get("public_task_id")),
            title=_task_title(task),
            latest_store_activity_at=latest_store_activity_at,
        )
        priority = _receipt_attention_priority(row)
        if priority is None:
            continue
        total += 1
        bucket = preview_by_priority[priority]
        if len(bucket) < DASHBOARD_RECEIPT_ATTENTION_LIMIT:
            bucket.append(row)

    preview = (preview_by_priority[0] + preview_by_priority[1])[
        :DASHBOARD_RECEIPT_ATTENTION_LIMIT
    ]
    return {
        "tasks": preview,
        "total": total,
        "limit": DASHBOARD_RECEIPT_ATTENTION_LIMIT,
        "truncated": len(preview) < total,
    }


def _finding_form_token(secret: str, event: Mapping[str, Any]) -> str | None:
    target_digest = finding_target_digest(event)
    if not secret or target_digest is None:
        return None
    material = f"finding-disposition-v1\0{target_digest}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()[:32]


def _evidence_work_fact_compatible(
    event: Mapping[str, Any],
    fact: Mapping[str, Any],
    *,
    allow_legacy_unscoped: bool,
) -> bool:
    event_actor = str(event.get("client") or event.get("source") or "")
    fact_actor = str(fact.get("client") or fact.get("reporting_source") or fact.get("source") or "")
    if event_actor and fact_actor and event_actor != fact_actor:
        return False
    event_project = str(event.get("project_identity") or "")
    fact_project = str(fact.get("project_identity") or "")
    if event_project or fact_project:
        if not event_project or not fact_project or event_project != fact_project:
            return False
    else:
        event_label = str(event.get("project_dir") or "")
        fact_label = str(fact.get("project_dir") or "")
        if event_label and fact_label and event_label != fact_label:
            return False
    if not namespace_join_compatible(
        event,
        fact,
        allow_legacy_unscoped=allow_legacy_unscoped,
    ):
        return False
    if event.get("log_evidence_conflict") or fact.get("log_evidence_conflict"):
        return False
    # A raw work/run hint is weaker than an explicit session or transcript
    # assertion. If both sides name that stronger identity and disagree, the
    # weaker hint must fail closed instead of redirecting the check.
    for key in ("client_session_id", "client_transcript_id"):
        event_value = str(event.get(key) or "")
        fact_value = str(fact.get(key) or "")
        if not event_value:
            continue
        if fact_value:
            if event_value != fact_value:
                return False
            continue
        candidate_values = {
            str(candidate.get(key) or "")
            for candidate in (
                fact.get("log_evidence_candidate_sessions")
                if isinstance(fact.get("log_evidence_candidate_sessions"), list)
                else []
            )
            if isinstance(candidate, Mapping) and candidate.get(key)
        }
        # An explicit event identity that the work/run fact cannot resolve is
        # a veto, not an invitation for the weaker raw ref to rescue the join.
        if event_value not in candidate_values:
            return False
    return True


def _link_mechanical_checks_by_session_time(projection: dict[str, Any]) -> dict[str, Any]:
    """Option A: credit a PASSING ``client_hook`` check (a hook-observed exit
    code) to the most recent CHECK-RELEVANT step begun in its session at or
    before the check's time — the work a test/build/lint actually exercises —
    which lifts that step from ``self_checked`` to ``independently_checked``.

    A hook check carries a ``client_session_id`` and a timestamp but NO section
    id (the harness sees the command, not which recorded step it was for), so
    ``_attach_evidence_to_task_projection`` leaves it at the task level. Two
    honesty guards keep this from misattributing:
      * only a PASSING hook check is placed — a failing one is never guessed onto
        a step (that would falsely demote an unrelated verified step); it stays
        task-level, visible on the decision axis.
      * only a check-relevant step (kind not research/review/planning/docs) is a
        candidate — a test never credits a docs step.
    A check that fits no eligible step (none had begun in that session yet) stays
    task-level and is disclosed as an unattributed check in the receipt ledger —
    never guessed onto an arbitrary step.
    """

    tasks = projection.get("tasks") if isinstance(projection.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        items = [item for item in (task.get("work_items") or []) if isinstance(item, dict)]
        if not items:
            continue
        by_session: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            # A test/build/lint only exercises check-relevant work — a docs or
            # planning step is never a candidate (crediting it would also make the
            # check vanish from the ledger, since a non-checkable step is excluded
            # from the tiers).
            if str(item.get("kind") or "unknown").lower() in NON_CHECK_RELEVANT_KINDS:
                continue
            session_id = str(item.get("client_session_id") or "")
            if session_id:
                by_session.setdefault(session_id, []).append(item)
        for group in by_session.values():
            group.sort(key=lambda item: float(item.get("started_at") or item.get("updated_at") or 0.0))
        # Gather the task-level hook checks from BOTH pools _attach populates
        # (a check may land in current_check_events and/or task_evidence_events);
        # the per-item append dedups, so seeing one twice is harmless.
        pool: list[Any] = []
        for pool_key in ("current_check_events", "task_evidence_events"):
            value = task.get(pool_key)
            if isinstance(value, list):
                pool.extend(value)
        for check in pool:
            if not isinstance(check, Mapping):
                continue
            if str(check.get("source_type") or "") != "client_hook":
                continue
            # Only positive proof is placed on a step; a failing hook check is
            # never guessed onto a step (that would falsely demote an unrelated
            # verified step) — it stays task-level, visible on the decision axis.
            if str(check.get("result") or "").lower() != "passed":
                continue
            session_id = str(check.get("client_session_id") or "")
            group = by_session.get(session_id)
            if not group:
                continue
            at = float(check.get("created_at") or check.get("occurred_at") or check.get("time") or 0.0)
            if at <= 0:
                continue
            active: dict[str, Any] | None = None
            for item in group:  # ascending by started_at
                if float(item.get("started_at") or item.get("updated_at") or 0.0) <= at:
                    active = item
                else:
                    break
            if active is None:
                continue
            current = active.get("current_check_events") if isinstance(active.get("current_check_events"), list) else []
            key = _evidence_event_key(check)
            if all(_evidence_event_key(existing) != key for existing in current):
                current.append(dict(check))
                active["current_check_events"] = current
    return projection


def _attach_evidence_to_task_projection(
    projection: dict[str, Any],
    evidence_events: list[dict[str, Any]],
    *,
    require_namespace_for_client_hook: bool = False,
) -> dict[str, Any]:
    """Project current check episodes before choosing Task/unassigned buckets."""

    tasks = projection.get("tasks") if isinstance(projection.get("tasks"), list) else []
    task_by_id = {
        str(task.get("task_id")): task
        for task in tasks
        if isinstance(task, dict) and task.get("task_id")
    }
    task_for_session: dict[tuple[str, str], str] = {}
    session_scope_for_key: dict[tuple[str, str], str] = {}
    mechanical_session_keys: set[tuple[str, str]] = set()
    work_facts_for_ref: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    task_facts_for_run: dict[tuple[str, str, str], list[tuple[str, dict[str, Any]]]] = {}
    namespace_facts_by_task: dict[str, list[Mapping[str, Any]]] = {}
    task_work_items_by_series: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unresolved_items_by_strict_series: dict[str, list[dict[str, Any]]] = {}

    # O(n) dedup instead of O(n^2): each target list gets a companion key set.
    # The lists live for the whole call and are only ever appended to here, so
    # id(rows) is a stable handle and the set stays in sync — same membership
    # test and same append order as the prior all(...) scan.
    _append_unique_seen: dict[int, set[tuple[str, ...]]] = {}

    def append_unique(rows: list[dict[str, Any]], event: Mapping[str, Any]) -> None:
        seen = _append_unique_seen.get(id(rows))
        if seen is None:
            seen = {_evidence_event_key(row) for row in rows}
            _append_unique_seen[id(rows)] = seen
        key = _evidence_event_key(event)
        if key not in seen:
            seen.add(key)
            rows.append(dict(event))

    for task_id, task in task_by_id.items():
        namespace_facts_by_task[task_id] = []
        task["task_evidence_events"] = []
        task["current_check_events"] = []
        task["open_finding_events"] = []
        work_evidence_keys: set[tuple[str, ...]] = set()
        for session in task.get("session_keys", []):
            if isinstance(session, Mapping):
                key = (str(session.get("client") or ""), str(session.get("client_session_id") or ""))
                if key[0] and key[1]:
                    task_for_session[key] = task_id
        for session in task.get("sessions", []):
            if not isinstance(session, Mapping):
                continue
            namespace_facts_by_task[task_id].append(session)
            key = (str(session.get("client") or ""), str(session.get("client_session_id") or ""))
            if not key[0] or not key[1]:
                continue
            fingerprint = str(session.get("namespace_fingerprint") or "")
            if fingerprint:
                session_scope_for_key[key] = fingerprint
            mechanical_capture = session.get("mechanical_capture") if isinstance(session.get("mechanical_capture"), Mapping) else {}
            if int(mechanical_capture.get("observation_count") or 0) > 0:
                mechanical_session_keys.add(key)
        for item_value in task.get("work_items", []):
            if not isinstance(item_value, dict):
                continue
            item = item_value
            item["current_check_events"] = []
            namespace_facts_by_task[task_id].append(item)
            for ref in (item.get("work_id"), item.get("section_id")):
                if ref:
                    work_facts_for_ref.setdefault(str(ref), []).append((task_id, item))
            run_id = str(item.get("run_id") or "")
            if run_id:
                run_key = (
                    str(item.get("client") or item.get("reporting_source") or "unknown"),
                    str(item.get("project_identity") or item.get("project_dir") or ""),
                    run_id,
                )
                task_facts_for_run.setdefault(run_key, []).append((task_id, item))
            item_events = item.get("evidence_events") if isinstance(item.get("evidence_events"), list) else []
            for event in item_events:
                if not isinstance(event, Mapping):
                    continue
                work_evidence_keys.add(_evidence_event_key(event))
                if str(event.get("result") or "").lower() in {"passed", "failed", "error"}:
                    series = finding_check_key(event, task_scoped=True)
                    task_work_items_by_series.setdefault((task_id, series), []).append(item)
        task["work_evidence_event_keys"] = work_evidence_keys

    unresolved_rows = projection.get("unresolved_work") if isinstance(projection.get("unresolved_work"), list) else []
    for unresolved in unresolved_rows:
        if not isinstance(unresolved, Mapping) or not isinstance(unresolved.get("item"), dict):
            continue
        item = unresolved["item"]
        item_events = item.get("evidence_events") if isinstance(item.get("evidence_events"), list) else []
        result_events = [
            event
            for event in item_events
            if isinstance(event, Mapping)
            and str(event.get("result") or "").lower() in {"passed", "failed", "error"}
        ]
        item["current_check_events"] = [dict(event) for event in latest_check_events(result_events)]
        for event in result_events:
            unresolved_items_by_strict_series.setdefault(
                finding_check_key(event), []
            ).append(item)

    allow_legacy_unscoped = not require_namespace_for_client_hook
    event_candidates: dict[tuple[str, ...], set[str]] = {}
    assignment_diagnostics: dict[tuple[str, ...], dict[str, Any]] = {}

    # namespace_join_compatible(event, fact) reads `fact` ONLY through its
    # namespace fingerprint and identity_scope_state=='explicit' (see
    # join_rules), so all(...) over a task's facts equals all(...) over its
    # facts collapsed to distinct (fingerprint, scope-explicit) signatures. A
    # single task often carries hundreds of same-signature facts; deduping here
    # turns the per-event all() from O(facts) into O(distinct signatures) with
    # an identical result. Built once — namespace_facts_by_task is complete.
    namespace_fact_reps: dict[str, list[Mapping[str, Any]]] = {}
    for _task_id, _facts in namespace_facts_by_task.items():
        _seen_signatures: set[tuple[str, bool]] = set()
        _reps: list[Mapping[str, Any]] = []
        for _fact in _facts:
            _signature = (
                str(_fact.get("namespace_fingerprint") or _fact.get("session_namespace_fingerprint") or "").strip(),
                str(_fact.get("identity_scope_state") or "").strip() == "explicit",
            )
            if _signature in _seen_signatures:
                continue
            _seen_signatures.add(_signature)
            _reps.append(_fact)
        namespace_fact_reps[_task_id] = _reps

    for event in evidence_events:
        if not isinstance(event, dict):
            continue
        candidate_task_ids: set[str] = set()
        observed_task_ids: set[str] = set()
        for ref in (event.get("work_id"), event.get("section_id")):
            facts = work_facts_for_ref.get(str(ref), []) if ref else []
            observed_task_ids.update(task_id for task_id, _fact in facts)
            compatible = {
                task_id
                for task_id, fact in facts
                if _evidence_work_fact_compatible(
                    event,
                    fact,
                    allow_legacy_unscoped=allow_legacy_unscoped,
                )
            }
            if len(compatible) == 1:
                candidate_task_ids.update(compatible)
        event_client = str(event.get("client") or event.get("source") or "")
        event_session = str(event.get("client_session_id") or "")
        if event_client and event_session:
            session_key = (event_client, event_session)
            event_fingerprint = str(event.get("namespace_fingerprint") or "")
            namespace_compatible = True
            if str(event.get("source_type") or "") == "client_hook":
                if event_fingerprint:
                    namespace_compatible = session_scope_for_key.get(session_key) == event_fingerprint
                elif require_namespace_for_client_hook:
                    namespace_compatible = session_key in mechanical_session_keys
            task_id = task_for_session.get(session_key) if namespace_compatible else None
            if task_id:
                observed_task_ids.add(task_id)
                if not event.get("log_evidence_conflict"):
                    candidate_task_ids.add(task_id)
        candidate_sessions = event.get("log_evidence_candidate_sessions")
        if isinstance(candidate_sessions, list) and candidate_sessions:
            candidate_rows = [
                candidate
                for candidate in candidate_sessions
                if isinstance(candidate, Mapping)
            ]
            all_donor_task_ids = {
                task_for_session.get((str(candidate.get("client") or ""), str(candidate.get("client_session_id") or "")))
                for candidate in candidate_rows
            }
            observed_task_ids.update(str(task_id) for task_id in all_donor_task_ids if task_id)
            explicit_candidate_keys = {
                key: str(event.get(key) or "")
                for key in ("client", "client_session_id", "client_transcript_id")
                if event.get(key)
            }
            compatible_candidate_rows = [
                candidate
                for candidate in candidate_rows
                if not event.get("log_evidence_conflict")
                and all(
                    str(candidate.get(key) or "") == expected
                    for key, expected in explicit_candidate_keys.items()
                )
            ]
            donor_task_ids = {
                task_for_session.get((str(candidate.get("client") or ""), str(candidate.get("client_session_id") or "")))
                for candidate in compatible_candidate_rows
            }
            if None not in donor_task_ids and len(donor_task_ids) == 1:
                candidate_task_ids.update(str(task_id) for task_id in donor_task_ids if task_id)
        run_id = str(event.get("run_id") or "")
        if run_id:
            run_key = (
                str(event.get("client") or event.get("source") or "unknown"),
                str(event.get("project_identity") or event.get("project_dir") or ""),
                run_id,
            )
            run_facts = task_facts_for_run.get(run_key, [])
            run_tasks = {task_id for task_id, _fact in run_facts}
            observed_task_ids.update(run_tasks)
            compatible_run_tasks = {
                task_id
                for task_id, fact in run_facts
                if _evidence_work_fact_compatible(
                    event,
                    fact,
                    allow_legacy_unscoped=allow_legacy_unscoped,
                )
            }
            if len(compatible_run_tasks) == 1:
                candidate_task_ids.update(compatible_run_tasks)
        candidate_task_ids = {
            task_id
            for task_id in candidate_task_ids
            if namespace_facts_by_task.get(task_id)
            and all(
                namespace_join_compatible(event, fact, allow_legacy_unscoped=allow_legacy_unscoped)
                for fact in namespace_fact_reps[task_id]
            )
        }
        event_key = _evidence_event_key(event)
        event_candidates[event_key] = candidate_task_ids
        assignment_diagnostics[event_key] = {
            "candidate_task_count": len(observed_task_ids),
            "compatible_task_count": len(candidate_task_ids),
        }

    check_events = [
        event
        for event in evidence_events
        if isinstance(event, dict) and str(event.get("result") or "").lower() in {"passed", "failed", "error"}
    ]
    task_scoped_groups: dict[str, list[dict[str, Any]]] = {}
    for event in check_events:
        task_scoped_groups.setdefault(
            finding_check_key(event, task_scoped=True), []
        ).append(event)

    unassigned_findings: list[dict[str, Any]] = []

    def record_unassigned(events: list[dict[str, Any]]) -> None:
        latest_rows = latest_check_events(events)
        if not latest_rows:
            return
        event = latest_rows[0]
        if str(event.get("result") or "").lower() not in {"failed", "error"}:
            return
        event_key = _evidence_event_key(event)
        diagnostic = assignment_diagnostics.get(event_key, {})
        raw_count = int(diagnostic.get("candidate_task_count") or 0)
        compatible_count = int(diagnostic.get("compatible_task_count") or 0)
        if raw_count > 1 or compatible_count > 1:
            assignment_state = "ambiguous_task_candidates"
            reason = "This check could belong to more than one Task. agentacct kept it unassigned instead of guessing."
        elif raw_count and not compatible_count:
            assignment_state = "namespace_mismatch"
            reason = "The possible Task context conflicts with this check's source, project, or namespace. agentacct refused the link."
        else:
            assignment_state = "no_task_candidate"
            reason = "This check has no deterministic Task context. agentacct kept the finding visible without inventing one."
        unassigned_findings.append(
            {
                "event": dict(event),
                "assignment_state": assignment_state,
                "reason": reason,
                "candidate_task_count": raw_count,
                "opened_at": event.get("created_at"),
                "updated_at": event.get("created_at"),
            }
        )

    def assign_series(events: list[dict[str, Any]], task_id: str, *, task_scoped: bool) -> None:
        task = task_by_id[task_id]
        latest_rows = latest_check_events(events, task_scoped=task_scoped)
        if not latest_rows:
            return
        latest = latest_rows[0]
        append_unique(task["current_check_events"], latest)
        if str(latest.get("result") or "").lower() in {"failed", "error"}:
            append_unique(task["open_finding_events"], latest)
        work_keys = task.get("work_evidence_event_keys") if isinstance(task.get("work_evidence_event_keys"), set) else set()
        for event in events:
            if _evidence_event_key(event) not in work_keys:
                append_unique(task["task_evidence_events"], event)
        series_keys = {
            finding_check_key(event, task_scoped=True)
            for event in events
        }
        for series in series_keys:
            for item in task_work_items_by_series.get((task_id, series), []):
                append_unique(item["current_check_events"], latest)

    def event_may_follow_task_scope(event: Mapping[str, Any], task_id: str) -> bool:
        event_key = _evidence_event_key(event)
        candidates = event_candidates.get(event_key, set())
        if candidates:
            return candidates == {task_id}
        diagnostic = assignment_diagnostics.get(event_key, {})
        if int(diagnostic.get("candidate_task_count") or 0) > 0:
            return False
        # A truly workspace-scoped check can follow a proven same-check retry
        # into the one Task in this series. An event that asserted a concrete
        # but unresolved session/work identity cannot: strict/unassigned is
        # safer than letting another event rescue the failed join.
        has_strong_unresolved_identity = any(
            event.get(key)
            for key in (
                "client_session_id",
                "client_transcript_id",
                "work_id",
                "section_id",
                "run_id",
                "log_evidence_conflict",
            )
        ) or bool(event.get("log_evidence_candidate_sessions"))
        return not has_strong_unresolved_identity

    for task_series, events in task_scoped_groups.items():
        union = {
            task_id
            for event in events
            for task_id in event_candidates.get(_evidence_event_key(event), set())
        }
        if len(union) == 1:
            task_id = next(iter(union))
            if all(event_may_follow_task_scope(event, task_id) for event in events):
                assign_series(events, task_id, task_scoped=True)
                continue
        strict_groups: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            strict_groups.setdefault(finding_check_key(event), []).append(event)
        for strict_series, strict_events in strict_groups.items():
            strict_union = {
                task_id
                for event in strict_events
                for task_id in event_candidates.get(_evidence_event_key(event), set())
            }
            if len(strict_union) == 1:
                assign_series(strict_events, next(iter(strict_union)), task_scoped=False)
                continue
            unresolved_items = unresolved_items_by_strict_series.get(strict_series, [])
            latest_rows = latest_check_events(strict_events)
            if unresolved_items and latest_rows:
                for item in unresolved_items:
                    append_unique(item["current_check_events"], latest_rows[0])
                continue
            record_unassigned(strict_events)

    # Non-result evidence remains historical context when it has one safe Task.
    for event in evidence_events:
        if not isinstance(event, dict) or str(event.get("result") or "").lower() in {"passed", "failed", "error"}:
            continue
        candidates = event_candidates.get(_evidence_event_key(event), set())
        if len(candidates) == 1:
            task = task_by_id[next(iter(candidates))]
            append_unique(task["task_evidence_events"], event)

    for task in task_by_id.values():
        task.pop("work_evidence_event_keys", None)
    for unresolved in unresolved_rows:
        if not isinstance(unresolved, Mapping) or not isinstance(unresolved.get("item"), dict):
            continue
        item = unresolved["item"]
        current = item.get("current_check_events") if isinstance(item.get("current_check_events"), list) else []
        current = [dict(event) for event in latest_check_events(current)]
        item["current_check_events"] = current
        item["open_finding_events"] = [
            event for event in current if str(event.get("result") or "").lower() in {"failed", "error"}
        ]

    unassigned_findings.sort(key=lambda finding: float(finding.get("updated_at") or 0.0), reverse=True)
    projection["unassigned_findings"] = unassigned_findings
    summary = projection.get("summary") if isinstance(projection.get("summary"), dict) else {}
    task_finding_count = sum(len(task.get("open_finding_events") or []) for task in task_by_id.values())
    unresolved_finding_count = sum(
        len(unresolved["item"].get("open_finding_events") or [])
        for unresolved in unresolved_rows
        if isinstance(unresolved, Mapping) and isinstance(unresolved.get("item"), Mapping)
    )
    projection["summary"] = {
        **summary,
        "assigned_open_finding_count": task_finding_count,
        "unresolved_open_finding_count": unresolved_finding_count,
        "unassigned_open_finding_count": len(unassigned_findings),
        "total_open_finding_count": task_finding_count + unresolved_finding_count + len(unassigned_findings),
    }
    return projection


def _apply_finding_dispositions_to_projection(
    projection: dict[str, Any],
    *,
    disposition_events: list[dict[str, Any]],
    form_secret: str,
) -> dict[str, Any]:
    """Overlay attention state without mutating objective machine checks."""

    disposition_projection = reduce_finding_dispositions(disposition_events)

    def episodes_for(
        events: Any,
        *,
        assignment: str,
        assignment_context: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(events, list):
            return []
        episodes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event in events:
            if not isinstance(event, Mapping):
                continue
            if str(event.get("result") or "").lower() not in {"failed", "error"}:
                continue
            target_digest = finding_target_digest(event)
            if target_digest is None or target_digest in seen:
                continue
            seen.add(target_digest)
            disposition = disposition_for_event(event, disposition_projection)
            token = _finding_form_token(form_secret, event)
            superseded = str(event.get("supersession_state") or "") == "superseded"
            # A superseded failure carries no attention on its own, but stays
            # reopenable: once the user has explicitly acted on it (revision > 0),
            # their disposition wins over the automatic supersession.
            attention_open = (disposition.state == "open") and (
                not superseded or int(disposition.revision or 0) > 0
            )
            episodes.append(
                {
                    "target_digest": target_digest,
                    "finding_token": token,
                    "objective_state": "current_failure",
                    "disposition_state": disposition.state,
                    "attention_open": attention_open,
                    "supersession_state": str(event.get("supersession_state") or "") or None,
                    "superseded_by_event_id": event.get("superseded_by_event_id"),
                    "supersession_basis": event.get("supersession_basis"),
                    "revision": disposition.revision,
                    "failure_event": dict(event),
                    "latest_disposition": disposition.to_dict(),
                    "opened_at": event.get("created_at"),
                    "assignment": assignment,
                    "assignment_context": dict(assignment_context or {}),
                }
            )
        return episodes

    tasks = projection.get("tasks") if isinstance(projection.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_scope = {
            "session_keys": [
                {
                    "client": str(key.get("client") or ""),
                    "client_session_id": str(key.get("client_session_id") or ""),
                }
                for key in (
                    task.get("session_keys")
                    if isinstance(task.get("session_keys"), list)
                    else []
                )
                if isinstance(key, Mapping)
                and key.get("client")
                and key.get("client_session_id")
            ],
            "work_ids": sorted(
                {
                    str(value)
                    for item in (
                        task.get("work_items")
                        if isinstance(task.get("work_items"), list)
                        else []
                    )
                    if isinstance(item, Mapping)
                    for value in (item.get("work_id"), item.get("section_id"))
                    if value
                }
            ),
            "run_ids": sorted(
                {
                    str(item.get("run_id"))
                    for item in (
                        task.get("work_items")
                        if isinstance(task.get("work_items"), list)
                        else []
                    )
                    if isinstance(item, Mapping) and item.get("run_id")
                }
            ),
        }
        task_episodes = episodes_for(
            task.get("current_check_events"),
            assignment="assigned",
            assignment_context={"task_id": task.get("task_id"), "task_scope": task_scope},
        )
        task["finding_episodes"] = task_episodes
        task["open_finding_events"] = [
            episode["failure_event"] for episode in task_episodes if episode["attention_open"]
        ]
        task["disposed_finding_events"] = [
            episode["failure_event"] for episode in task_episodes if not episode["attention_open"]
        ]
        for item in task.get("work_items", []):
            if not isinstance(item, dict):
                continue
            item_episodes = episodes_for(
                item.get("current_check_events"),
                assignment="assigned",
                assignment_context={
                    "task_id": task.get("task_id"),
                    "work_id": item.get("work_id"),
                    "task_scope": task_scope,
                },
            )
            item["finding_episodes"] = item_episodes
            item["open_finding_events"] = [
                episode["failure_event"] for episode in item_episodes if episode["attention_open"]
            ]

    unresolved_rows = (
        projection.get("unresolved_work")
        if isinstance(projection.get("unresolved_work"), list)
        else []
    )
    for unresolved in unresolved_rows:
        if not isinstance(unresolved, Mapping) or not isinstance(unresolved.get("item"), dict):
            continue
        item = unresolved["item"]
        item_episodes = episodes_for(
            item.get("current_check_events"),
            assignment="unresolved",
            assignment_context={
                "reason": unresolved.get("reason"),
                "work_id": item.get("work_id"),
            },
        )
        item["finding_episodes"] = item_episodes
        item["open_finding_events"] = [
            episode["failure_event"] for episode in item_episodes if episode["attention_open"]
        ]

    unassigned_rows = (
        projection.get("unassigned_findings")
        if isinstance(projection.get("unassigned_findings"), list)
        else []
    )
    open_unassigned: list[dict[str, Any]] = []
    disposed_unassigned: list[dict[str, Any]] = []
    for finding in unassigned_rows:
        if not isinstance(finding, Mapping) or not isinstance(finding.get("event"), Mapping):
            continue
        episode_rows = episodes_for(
            [finding["event"]],
            assignment="unassigned",
            assignment_context={
                "assignment_state": finding.get("assignment_state"),
                "reason": finding.get("reason"),
            },
        )
        if not episode_rows:
            continue
        row = {**dict(finding), "episode": episode_rows[0]}
        if episode_rows[0]["attention_open"]:
            open_unassigned.append(row)
        else:
            disposed_unassigned.append(row)
    projection["unassigned_findings"] = open_unassigned
    projection["disposed_unassigned_findings"] = disposed_unassigned

    task_episodes = [
        episode
        for task in tasks
        if isinstance(task, Mapping)
        for episode in (
            task.get("finding_episodes")
            if isinstance(task.get("finding_episodes"), list)
            else []
        )
        if isinstance(episode, Mapping)
    ]
    unresolved_episodes = [
        episode
        for unresolved in unresolved_rows
        if isinstance(unresolved, Mapping) and isinstance(unresolved.get("item"), Mapping)
        for episode in (
            unresolved["item"].get("finding_episodes")
            if isinstance(unresolved["item"].get("finding_episodes"), list)
            else []
        )
        if isinstance(episode, Mapping)
    ]
    unassigned_episodes = [
        finding["episode"]
        for finding in [*open_unassigned, *disposed_unassigned]
        if isinstance(finding.get("episode"), Mapping)
    ]
    all_episodes = [*task_episodes, *unresolved_episodes, *unassigned_episodes]
    open_count = sum(bool(episode.get("attention_open")) for episode in all_episodes)
    reviewed_count = sum(episode.get("disposition_state") == "reviewed" for episode in all_episodes)
    resolved_count = sum(episode.get("disposition_state") == "resolved" for episode in all_episodes)
    # A superseded finding gets its own bucket and count -- never folded into
    # "Open findings", never dropped from history.
    superseded_count = sum(
        str(episode.get("supersession_state") or "") == "superseded" and not episode.get("attention_open")
        for episode in all_episodes
    )
    summary = projection.get("summary") if isinstance(projection.get("summary"), dict) else {}
    projection["summary"] = {
        **summary,
        "assigned_open_finding_count": sum(
            bool(episode.get("attention_open")) for episode in task_episodes
        ),
        "unresolved_open_finding_count": sum(
            bool(episode.get("attention_open")) for episode in unresolved_episodes
        ),
        "unassigned_open_finding_count": len(open_unassigned),
        "total_open_finding_count": open_count,
        "superseded_finding_count": superseded_count,
        "current_finding_count": len(all_episodes),
        "reviewed_finding_count": reviewed_count,
        "resolved_finding_count": resolved_count,
    }
    projection["finding_disposition_diagnostics"] = disposition_projection.diagnostics
    return projection


def _stamp_task_plan_shares(projection: Mapping[str, Any], events: list[dict[str, Any]]) -> None:
    """Stamp each projected Task with its weekly-plan share.

    A Task's share is the SUM of its member sessions' calibrated per-session
    plan percentages — raw per-session values (never the root-folded display
    numbers), so members are counted exactly once. Uses the same one-shot
    computation the glance lane uses (``plan_status_and_session_pcts``), so a
    receipt and the menu bar can never disagree about a session's share.
    Calibrated-or-nothing: with no calibrated fit for the Task's client the
    ``pct`` is null and ``calibration_state`` says why — never a fabricated 0.
    ``covered_sessions`` names how many members actually carried a share, so a
    partial sum is disclosed instead of passed off as complete.
    """

    from .glance import plan_status_and_session_pcts

    plan_entries, session_pcts, _weights, _records = plan_status_and_session_pcts(events)
    states = {
        str(entry.get("client") or ""): str(entry.get("calibration_state") or "") or None
        for entry in plan_entries
        if isinstance(entry, Mapping)
    }
    for task in projection.get("tasks", []):
        if not isinstance(task, dict):
            continue
        members = [
            member
            for member in (task.get("session_keys") or [])
            if isinstance(member, Mapping)
        ]
        primary = task.get("primary_root")
        client = str(primary.get("client") or "") if isinstance(primary, Mapping) else ""
        covered = 0
        total_pct = 0.0
        for member in members:
            member_client = str(member.get("client") or "")
            # One client, one plan: the share is labelled with the primary
            # root's client, so only that client's member sessions may
            # contribute — a cross-client continuation must never sum another
            # plan's percentages under this label.
            if member_client != client:
                continue
            key = (member_client, str(member.get("client_session_id") or ""))
            pct = session_pcts.get(key)
            if pct is not None:
                covered += 1
                total_pct += float(pct)
        # A client outside the plan lane (hermes/opencode/...) is honestly
        # "never" — its meter has no calibratable weekly plan — instead of a
        # null that reads as "unknown".
        state = states.get(client) or ("never" if client else None)
        task["plan_share"] = {
            "pct": total_pct if covered else None,
            "client": client or None,
            "calibration_state": state,
            "covered_sessions": covered,
            "session_count": len(members),
        }


def _dashboard_task_projection(data: _DashboardPageData) -> dict[str, Any]:
    sessions = data.rollup_sessions
    work_items = data.work_items
    excluded_session_keys: set[tuple[str, str]] = set()
    excluded_transcript_keys: set[tuple[str, str]] = set()
    excluded_namespace_keys: set[tuple[str, str]] = set()
    excluded_source_namespace_keys: set[tuple[str, str]] = set()
    excluded_run_keys: set[tuple[str, str]] = set()
    excluded_work_refs: set[tuple[str, str]] = set()

    def remember_excluded_fact(fact: Mapping[str, Any]) -> None:
        actor = str(fact.get("client") or fact.get("reporting_source") or fact.get("source") or "")
        if not actor:
            return
        for value, target in (
            (fact.get("client_session_id"), excluded_session_keys),
            (fact.get("client_transcript_id"), excluded_transcript_keys),
            (
                fact.get("namespace_fingerprint") or fact.get("session_namespace_fingerprint"),
                excluded_namespace_keys,
            ),
            (fact.get("source_namespace_fingerprint"), excluded_source_namespace_keys),
            (fact.get("run_id"), excluded_run_keys),
        ):
            if value:
                target.add((actor, str(value)))
    if data.store_scope == "project":
        scoped_sessions: list[Mapping[str, Any]] = []
        for session in sessions:
            if not isinstance(session, Mapping):
                continue
            in_scope = _overview_project_in_scope(
                data,
                session.get("project"),
                session.get("project_identity"),
                session.get("project_identity_state"),
            )
            if in_scope:
                scoped_sessions.append(session)
                continue
            remember_excluded_fact(session)
        sessions = scoped_sessions

        scoped_work_items: list[Mapping[str, Any]] = []
        for item in work_items:
            if not isinstance(item, Mapping):
                continue
            in_scope = _overview_project_in_scope(
                data,
                item.get("project_dir"),
                item.get("project_identity"),
            )
            if in_scope:
                scoped_work_items.append(item)
                continue
            remember_excluded_fact(item)
            actor = str(item.get("client") or item.get("reporting_source") or "")
            for ref in (item.get("work_id"), item.get("section_id")):
                if actor and ref:
                    excluded_work_refs.add((actor, str(ref)))
        work_items = scoped_work_items
    projection = build_task_projection(
        sessions,
        work_items,
        continuation_memberships=data.continuation_projection,
    )
    evidence_events = data.ledger.get("evidence_events")
    all_evidence_events = [
        *(evidence_events if isinstance(evidence_events, list) else []),
        *data.mechanical_check_events,
    ]
    if data.store_scope == "project":
        scoped_evidence_events: list[dict[str, Any]] = []
        for event in all_evidence_events:
            if not isinstance(event, dict):
                continue
            event_identity = str(event.get("project_identity") or "").strip()
            if event_identity:
                if _overview_project_in_scope(
                    data,
                    event.get("project_dir"),
                    event_identity,
                ):
                    scoped_evidence_events.append(event)
                continue
            actor = str(event.get("client") or event.get("source") or "")
            session_key = (actor, str(event.get("client_session_id") or ""))
            transcript_key = (actor, str(event.get("client_transcript_id") or ""))
            namespace_key = (
                actor,
                str(event.get("namespace_fingerprint") or event.get("session_namespace_fingerprint") or ""),
            )
            source_namespace_key = (actor, str(event.get("source_namespace_fingerprint") or ""))
            run_key = (actor, str(event.get("run_id") or ""))
            excluded_by_session = bool(
                session_key[0] and session_key[1] and session_key in excluded_session_keys
            )
            excluded_by_transcript = bool(
                transcript_key[0]
                and transcript_key[1]
                and transcript_key in excluded_transcript_keys
            )
            excluded_by_namespace = bool(
                namespace_key[0]
                and namespace_key[1]
                and namespace_key in excluded_namespace_keys
            ) or bool(
                source_namespace_key[0]
                and source_namespace_key[1]
                and source_namespace_key in excluded_source_namespace_keys
            )
            excluded_by_run = bool(run_key[0] and run_key[1] and run_key in excluded_run_keys)
            excluded_by_work = any(
                actor and ref and (actor, str(ref)) in excluded_work_refs
                for ref in (event.get("work_id"), event.get("section_id"))
            )
            candidate_sessions = (
                event.get("log_evidence_candidate_sessions")
                if isinstance(event.get("log_evidence_candidate_sessions"), list)
                else []
            )
            excluded_by_candidate = any(
                isinstance(candidate, Mapping)
                and (
                    str(candidate.get("client") or ""),
                    str(candidate.get("client_session_id") or ""),
                )
                in excluded_session_keys
                for candidate in candidate_sessions
            )
            if (
                excluded_by_session
                or excluded_by_transcript
                or excluded_by_namespace
                or excluded_by_run
                or excluded_by_work
                or excluded_by_candidate
            ):
                continue
            scoped_evidence_events.append(event)
        all_evidence_events = scoped_evidence_events
    projection = _attach_evidence_to_task_projection(
        projection,
        all_evidence_events,
        require_namespace_for_client_hook=data.store_scope != "project",
    )
    # Option A: place hook-observed checks (no section id) on the step active in
    # their session at the time they ran, so a real test/build/lint lifts that
    # step to independently_checked; unplaceable ones stay disclosed as
    # unattributed in the ledger.
    projection = _link_mechanical_checks_by_session_time(projection)
    if data.store_scope == "project":
        unassigned = projection.get("unassigned_findings")
        if isinstance(unassigned, list):
            scoped_unassigned = [
                finding
                for finding in unassigned
                if isinstance(finding, Mapping)
                and isinstance(finding.get("event"), Mapping)
                and _overview_project_in_scope(
                    data,
                    finding["event"].get("project_dir"),
                    finding["event"].get("project_identity"),
                )
            ]
            projection["unassigned_findings"] = scoped_unassigned
            summary = projection.get("summary") if isinstance(projection.get("summary"), dict) else {}
            projection["summary"] = {
                **summary,
                "unassigned_open_finding_count": len(scoped_unassigned),
                "total_open_finding_count": (
                    int(summary.get("assigned_open_finding_count") or 0)
                    + int(summary.get("unresolved_open_finding_count") or 0)
                    + len(scoped_unassigned)
                ),
            }
    projection = _apply_finding_dispositions_to_projection(
        projection,
        disposition_events=data.events,
        form_secret=data.task_csrf_token,
    )
    _apply_blocker_dispositions_to_projection(projection, events=data.events)
    return data.task_identity.decorate_projection(projection) if data.task_identity is not None else projection


def _apply_blocker_dispositions_to_projection(
    projection: Mapping[str, Any], *, events: list[dict[str, Any]]
) -> None:
    """Stamp each blocked work item with its human attention disposition.

    Replays the blocker-attention chains (the finding machinery with the
    blocker event type) and, for every item whose ``current_blocked_event_id``
    resolves to a raw ledger event, stamps the item with the chain's state:

    * ``blocker_disposition_state`` — open/reviewed/resolved (absent = open);
    * ``blocker_disposition`` — {state, revision, note, updated_at} for the UI;
    * ``blocker_target_revision`` — the optimistic-concurrency revision a
      write must echo.

    Read-time only; the agent's recorded events are never touched. The outcome
    reducer consumes the stamp so a human-resolved blocker stops forcing the
    Task's decision word (the machine resolution lane stays separate).
    """

    from .finding_disposition import (
        BLOCKER_DISPOSITION_AUTHORITY_SCOPE,
        BLOCKER_DISPOSITION_EVENT_TYPE,
        finding_target_digest,
        reduce_finding_dispositions,
    )

    chains = reduce_finding_dispositions(
        events,
        event_type=BLOCKER_DISPOSITION_EVENT_TYPE,
        authority_scope=BLOCKER_DISPOSITION_AUTHORITY_SCOPE,
    )
    wanted_ids: set[str] = set()
    for task in projection.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        for item in task.get("work_items", []) or []:
            if isinstance(item, Mapping):
                blocked_id = str(item.get("current_blocked_event_id") or "")
                if blocked_id:
                    wanted_ids.add(blocked_id)
    if not wanted_ids:
        return
    raw_by_id = {
        str(event.get("event_id") or ""): event
        for event in events
        if str(event.get("event_id") or "") in wanted_ids
    }
    for task in projection.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        for item in task.get("work_items", []) or []:
            if not isinstance(item, dict):
                continue
            blocked_id = str(item.get("current_blocked_event_id") or "")
            raw = raw_by_id.get(blocked_id)
            if raw is None:
                continue
            digest = finding_target_digest(raw)
            if digest is None:
                continue
            state = chains.states.get(digest)
            revision = state.revision if state is not None else 0
            item["blocker_target_revision"] = revision
            if state is None or digest in chains.invalid_targets:
                continue
            item["blocker_disposition_state"] = state.state
            item["blocker_disposition"] = {
                "state": state.state,
                "revision": state.revision,
                "note": state.note,
                "updated_at": state.updated_at,
            }


# ---------------------------------------------------------------------------
# Phase 3.5c — the Sessions explorer filters (PRD §6.2). Same rules as the
# /tokens filters: whitelisted choices (unknown values fall back to their
# defaults, never a 500), project validated against labels actually present
# (an unknown label renders the EMPTY result with the filter echoed — never a
# guess), every combination a URL, and the 40-row cap applies AFTER the
# filters with the honest "Showing N of M (filtered from T)" restatement.
# ---------------------------------------------------------------------------

# /sessions display order: newest-first browse (default) vs the attribution-first
# triage order shared with the overview data. Kept separate from
# _session_display_sort_key so changing the browse order never moves other views.
# Optional lens: only sessions with recorded agentacct work (sections/checks).
# The browse can page past the default slice; "Show more" raises the cap.


# ---------------------------------------------------------------------------
# Store-direct assembly (shared by the HTTP routes and the local write lanes)
# ---------------------------------------------------------------------------


# ONE run-report cap for every ledger build. The self-build path
# (build_page_data -> /tasks, the CLI, and the /v1 task lane's reference) and
# the shared derived ledger (the /v1 sessions/tasks lane) MUST pass the same
# value, or the two would agree only on stores with fewer reports than the
# smaller cap — the /v1 task lane reuses the derived ledger, so a mismatch
# would silently change what receipts show once a store accrues run reports.
# Run reports become work-item evidence, so this is a correctness cap, not a
# display nicety. (Under the MCP-first layout runs/ is empty and the cap is
# inert; it matters only for stores fed by the `agentacct run` flow.)
_LEDGER_RUN_REPORT_LIMIT = 100

# Ledger dict key under which the derived-ledger builder stashes the mechanical
# check events it derived from the Evidence store, so the warm /v1 page assembly
# can attach them without re-reading the Evidence store. Present only on ledgers
# built by the app's _derived_work_ledger; absent from a bare build_work_ledger.
_LEDGER_MECHANICAL_CHECK_EVENTS_KEY = "__page_mechanical_check_events__"


def _collect_service_run_reports(service: SentinelService, *, limit: int = _LEDGER_RUN_REPORT_LIMIT) -> list[dict[str, Any]]:
    reports = []
    for run in service.list_runs(limit=limit):
        run_id = run.get("run_id")
        if not run_id:
            continue
        try:
            reports.append(service.get_report(str(run_id)))
        except (FileNotFoundError, ValueError):
            continue
    return reports


def _mechanical_projection_envelopes_for(
    service: SentinelService, store_dir: Path | str
) -> tuple[list[Any], dict[str, int]]:
    """Read valid hook projection inputs without mutating the v1 store.

    EvidenceRuntime is intentionally lazy; a normal read must not create
    Evidence v2 storage in a fresh store.  Any projection/read failure is
    additive-only and therefore fail-open.
    """

    evidence_root = Path(store_dir).expanduser() / EVIDENCE_STORE_DIRNAME
    diagnostics: dict[str, int] = {}
    if not service.evidence.enabled or not evidence_root.exists():
        return [], diagnostics
    try:
        # Product reads are bounded: project the most recent mechanical window
        # and report a diagnostic when that window may be truncated. Query
        # every assertion so conflict decisions see the complete idempotency
        # groups present inside the window.
        records = service.evidence.recent_records(
            limit=MECHANICAL_PROJECTION_LIMIT,
            source_type="client_hook",
        )
        if not records:
            return [], {}
        diagnostics["window_limit"] = MECHANICAL_PROJECTION_LIMIT
        diagnostics["history_window_maybe_truncated"] = int(
            len(records) >= MECHANICAL_PROJECTION_LIMIT
        )
        records, complete_conflict_keys = expand_complete_conflict_groups(
            records,
            load_group=lambda idempotency_key, limit: service.evidence.records(
                limit=limit,
                order_by="arrival",
                idempotency_key=idempotency_key,
            ),
            group_limit=MECHANICAL_CONFLICT_GROUP_LIMIT,
            row_limit=MECHANICAL_CONFLICT_ROW_LIMIT,
            diagnostics=diagnostics,
        )
        envelopes = select_session_projection_envelopes(
            records,
            complete_conflict_keys=complete_conflict_keys,
            diagnostics=diagnostics,
        )
        return envelopes, diagnostics
    except Exception:  # noqa: BLE001 - additive Evidence v2 cannot break reads.
        diagnostics["read_errors"] = int(diagnostics.get("read_errors") or 0) + 1
        return [], diagnostics


def build_page_data(
    store_dir: Path | str,
    *,
    service: SentinelService | None = None,
    cost_ledger: CostLedger | None = None,
    continuation_store: ContinuationTaskStore | None = None,
    task_identity: TaskIdentityCodec | None = None,
    ingestion_health_store: IngestionHealthStore | None = None,
    local_usage_preview: list[ClientUsageEvent] | None = None,
    continuation_snapshot: Mapping[str, Any] | None = None,
    task_csrf_token: str = "",
    events: list[dict[str, Any]] | None = None,
    ledger: dict[str, Any] | None = None,
) -> _DashboardPageData:
    """The saved-rows page context, buildable directly from a store.

    One assembly for the HTTP routes (the app factory passes its cached
    stores) and the local write lanes (`agentacct task …` / `agentacct
    finding …` construct per call): both resolve against the SAME
    scope-quarantined projection, so a write lane can never see — or
    target — a finding the read surface would not show.
    """

    service = service or SentinelService(store_dir)
    cost_ledger = cost_ledger or CostLedger(store_dir)
    continuation_store = continuation_store or ContinuationTaskStore(store_dir)
    task_identity = task_identity or TaskIdentityCodec(store_dir)
    ingestion_health_store = ingestion_health_store or IngestionHealthStore(store_dir)
    store_scope, store_project_label = _store_scope_and_label(store_dir)
    store_project_identity = (
        _project_identity(str(Path(store_dir).expanduser().parent.parent))
        if store_scope == "project"
        else None
    )
    store_display_label = _store_display_label(store_dir, store_scope, store_project_label)
    # A caller that already loaded events for THIS build (the /v1 task lane,
    # which also passes the matching pre-built ``ledger``) hands them in so the
    # page's usage view and mechanical checks reflect the SAME snapshot the
    # injected ledger was reduced from — not a second, possibly newer read.
    events = events if events is not None else service.list_all_events()
    # When a pre-built ledger is injected AND it carries the mechanical check
    # events its own build derived from the Evidence store (the /v1 task lane),
    # reuse them instead of reading the Evidence store again (~350ms). The
    # session observations and run reports only feed build_work_ledger, which is
    # skipped on an injected ledger, so they are dead work here too. On the
    # self-build path (ledger is None) everything is computed as before.
    stashed_checks = ledger.get(_LEDGER_MECHANICAL_CHECK_EVENTS_KEY) if ledger is not None else None
    if stashed_checks is not None:
        mechanical_check_events = stashed_checks
        session_observations = []
        observation_diagnostics = {}
        run_reports = []
    else:
        mechanical_envelopes, observation_diagnostics = _mechanical_projection_envelopes_for(service, store_dir)
        session_observations = (
            build_session_observations(
                mechanical_envelopes,
                default_project_label=store_project_label if store_scope == "project" else None,
                diagnostics=observation_diagnostics,
            )
            if mechanical_envelopes
            else []
        )
        run_reports = _collect_service_run_reports(service, limit=_LEDGER_RUN_REPORT_LIMIT)
        mechanical_check_events = build_mechanical_check_events(mechanical_envelopes)
    cost_events = sorted(cost_ledger.read_events(), key=lambda item: float(item.get("created_at") or 0.0), reverse=True)
    return _dashboard_page_data(
        events=events,
        cost_events=cost_events,
        run_reports=run_reports,
        session_observations=session_observations,
        session_observation_diagnostics=observation_diagnostics,
        mechanical_check_events=mechanical_check_events,
        store_project_label=store_project_label,
        store_project_identity=store_project_identity,
        store_scope=store_scope,
        store_label=store_display_label,
        local_usage_preview=local_usage_preview,
        continuation_projection=(
            dict(continuation_snapshot)
            if continuation_snapshot is not None
            else continuation_store.project().to_dict()
        ),
        task_csrf_token=task_csrf_token,
        ingestion_health=ingestion_health_store.snapshot(),
        task_identity=task_identity,
        ledger=ledger,
    )


def build_store_task_projection(
    store_dir: Path | str,
    *,
    continuation_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The GET /tasks projection built directly from a store (no HTTP).

    Named distinctly from task_projection.build_task_projection (the
    event-level reducer this assembly ultimately calls through the page-data
    path) — shadowing that import broke the /tasks route once already."""

    data = build_page_data(store_dir, continuation_snapshot=continuation_snapshot)
    projection = _dashboard_task_projection(data)
    # Same stamp the /v1 lane applies, so `agentacct receipt` / the TUI can
    # never disagree with the app about a task's weekly-plan share.
    _stamp_task_plan_shares(projection, data.events)
    return projection


def surfaced_finding_episodes(projection: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every finding episode the projection actually surfaces.

    This is the canonical, scope-quarantined index: a hidden foreign-project
    finding never appears here, so a resolver walking this list inherits the
    same refusal the retired form resolver enforced.
    """

    episodes: list[Mapping[str, Any]] = []
    for task in projection.get("tasks", []):
        if isinstance(task, Mapping) and isinstance(task.get("finding_episodes"), list):
            episodes.extend(
                episode for episode in task["finding_episodes"] if isinstance(episode, Mapping)
            )
    for unresolved in projection.get("unresolved_work", []):
        item = unresolved.get("item") if isinstance(unresolved, Mapping) else None
        if isinstance(item, Mapping) and isinstance(item.get("finding_episodes"), list):
            episodes.extend(
                episode for episode in item["finding_episodes"] if isinstance(episode, Mapping)
            )
    for key in ("unassigned_findings", "disposed_unassigned_findings"):
        for finding in projection.get(key, []):
            episode = finding.get("episode") if isinstance(finding, Mapping) else None
            if isinstance(episode, Mapping):
                episodes.append(episode)
    return episodes


def resolve_finding_episode(projection: Mapping[str, Any], *, digest: str) -> Mapping[str, Any]:
    """The surfaced episode whose target digest matches, or NotFound.

    Resolves ONLY from the scope-quarantined surfaced index (never from the
    raw evidence store), mirroring the retired form resolver: a validly
    crafted digest for a hidden foreign-project finding still gets NotFound.
    """

    cleaned = str(digest or "").strip().lower()
    if not cleaned:
        raise FindingDispositionNotFound("finding digest is required")
    matches: dict[str, Mapping[str, Any]] = {}
    for episode in surfaced_finding_episodes(projection):
        event = episode.get("failure_event") if isinstance(episode.get("failure_event"), Mapping) else None
        if event is None:
            continue
        target_digest = finding_target_digest(event)
        if target_digest is None:
            continue
        if str(target_digest).lower().startswith(cleaned):
            matches[str(target_digest)] = episode
    if not matches:
        raise FindingDispositionNotFound("no surfaced finding matches that digest")
    if len(matches) > 1:
        raise FindingDispositionNotFound(
            "finding digest prefix is ambiguous; pass more characters"
        )
    return next(iter(matches.values()))


def create_local_api_app(
    *,
    store_dir: Path | str,
    usage_discovery: UsageDiscoveryConfig | None = None,
    extra_allowed_hosts: Iterable[str] = (),
    v1_auth_token: str | None = None,
) -> FastAPI:
    """Create the local-only agentacct API used by sidecar/MCP surfaces.

    This app exposes report/outcome/event primitives only. It does not call paid
    LLM judges or mutate external agent tools. Bind it to 127.0.0.1 for local use.
    Callers must resolve the store first (no silent home-store default).

    ``v1_auth_token`` arms the ``/v1`` native-shell lane (glance/version). It is
    per-boot: ``agentacct serve`` generates one and publishes it via the 0600
    discovery file (see :mod:`agentacct.glance`). Without a token the /v1 routes
    fail closed (503) — they never open an unauthenticated lane by accident.
    """
    app = FastAPI(title="agentacct local api", version="0.1.0")
    app_pricing_catalog_path = pricing_catalog_path_for_store(store_dir)
    usage_discovery = usage_discovery or UsageDiscoveryConfig()

    @app.middleware("http")
    async def _pricing_catalog_snapshot_middleware(request: Any, call_next: Any) -> Any:
        previous_catalog_path = os.environ.get(PRICING_CATALOG_PATH_ENV)
        # The is-a-catalog-already-pinned check honors BOTH env names via
        # read_env_alias: a pre-rename AGENT_SENTINEL_* pin must keep winning
        # over the store snapshot exactly as the new name would. Only the
        # override/restore below mutates (and only ever mutates) the new name.
        user_pinned = read_env_alias(PRICING_CATALOG_PATH_ENV) is not None
        # Stashed BEFORE this middleware's own per-request pin so the usage
        # refresh route can tell a USER pin (never auto-refresh over it) from
        # the store-snapshot pin this middleware is about to apply.
        request.state.pricing_catalog_user_pinned = user_pinned
        if not user_pinned and app_pricing_catalog_path is not None and app_pricing_catalog_path.exists():
            os.environ[PRICING_CATALOG_PATH_ENV] = str(app_pricing_catalog_path)
            reset_pricing_catalog_cache()
        try:
            return await call_next(request)
        finally:
            if previous_catalog_path is None:
                os.environ.pop(PRICING_CATALOG_PATH_ENV, None)
            else:
                os.environ[PRICING_CATALOG_PATH_ENV] = previous_catalog_path
            reset_pricing_catalog_cache()

    # Added AFTER the pricing middleware so the guard runs FIRST (Starlette
    # runs the most recently added middleware outermost): rejected cross-site
    # requests never reach the pricing env-var swap or any route handler.
    install_localhost_guard(app, extra_allowed_hosts=extra_allowed_hosts)

    service = SentinelService(store_dir)
    cost_ledger = CostLedger(store_dir)
    ingestion_health = IngestionHealthStore(store_dir)
    continuation_store = ContinuationTaskStore(store_dir)
    task_identity = TaskIdentityCodec(store_dir)
    activation_store = ActivationStateStore(store_dir)
    task_csrf_token = secrets.token_urlsafe(32)
    control_store = ControlStore(store_dir)
    control_supervisor = OwnedSupervisor(store_dir, control_store=control_store)
    app.state.control_supervisor_error = None

    def _recover_owned_control_attempts() -> None:
        """Recover only durable agentacct-owned attempts, never external work."""

        if not control_store.actions_path.exists():
            return
        try:
            projection = control_store.project()
            if any(
                row.execution_state in {"launching", "running", "cancel_requested"}
                for row in projection.attempts.values()
            ):
                control_supervisor.start()
                control_supervisor.reconcile()
        except (ControlPlaneError, SupervisorError, OSError):
            # The product page reports a bounded availability state; raw
            # process/store details remain private to local diagnostics.
            app.state.control_supervisor_error = "recovery_unavailable"

    def _close_owned_control_supervisor() -> None:
        control_supervisor.close()

    app.router.add_event_handler("startup", _recover_owned_control_attempts)
    app.router.add_event_handler("shutdown", _close_owned_control_supervisor)
    # Derived ONCE from the store dir this app was constructed with: the
    # dashboard's own scope (EXPLICIT "project" | "custom") and project
    # label, for cross-store context_scope honesty. On a custom/global store
    # the ledger never claims a session's context lives in another project's
    # store — that claim only makes sense for per-project stores.
    store_scope, store_project_label = _store_scope_and_label(store_dir)
    store_project_identity = (
        # Match the historical-path identity contract: this is a pure textual
        # identity and must not resolve filesystem aliases here when event
        # project paths are intentionally never resolved at read time.
        _project_identity(str(Path(store_dir).expanduser().parent.parent))
        if store_scope == "project"
        else None
    )
    # Identity-strip display label (redacted: leaf segments only, never an
    # absolute path) — derived once alongside the scope.
    store_display_label = _store_display_label(store_dir, store_scope, store_project_label)

    def _not_found(exc: FileNotFoundError) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc))

    def _invalid(exc: ValueError) -> HTTPException:
        return HTTPException(status_code=422, detail=str(exc))

    def _collect_run_reports(limit: int = _LEDGER_RUN_REPORT_LIMIT) -> list[dict[str, Any]]:
        return _collect_service_run_reports(service, limit=limit)

    def _mechanical_projection_envelopes() -> tuple[list[Any], dict[str, int]]:
        return _mechanical_projection_envelopes_for(service, store_dir)

    ledger_cache = WorkLedgerCache()

    def _derived_work_ledger(
        events: list[dict[str, Any]] | None = None,
        *,
        fingerprint: int | None = None,
    ) -> dict[str, Any]:
        """The derived ledger, fingerprint + TTL cached (see WorkLedgerCache).

        Every ledger-backed route shares one cache: a poll that finds no event
        change pays one store read + an O(n) hash, never the multi-second
        rebuild. ``events``/``fingerprint`` may be passed pre-computed by a
        route that already loaded them (avoids a second store read/hash).
        """

        if events is None:
            events = service.list_all_events()
        if fingerprint is None:
            fingerprint = events_fingerprint(events)
        loaded_events = events

        def _build() -> dict[str, Any]:
            mechanical_envelopes, observation_diagnostics = _mechanical_projection_envelopes()
            session_observations = (
                build_session_observations(
                    mechanical_envelopes,
                    default_project_label=store_project_label if store_scope == "project" else None,
                    diagnostics=observation_diagnostics,
                )
                if mechanical_envelopes
                else []
            )
            ledger = build_work_ledger(
                loaded_events,
                run_reports=_collect_run_reports(limit=_LEDGER_RUN_REPORT_LIMIT),
                cost_events=cost_ledger.read_events(),
                session_observations=session_observations,
                session_observation_diagnostics=observation_diagnostics,
                store_project_label=store_project_label,
                store_scope=store_scope,
            )
            # Stash the mechanical check events derived from the envelopes this
            # build already loaded, so build_page_data's warm /v1 path can attach
            # them without paying the ~350ms Evidence-store read a second time.
            # Same fingerprint + TTL staleness bound WorkLedgerCache already
            # applies to its other Evidence-derived secondary inputs.
            ledger[_LEDGER_MECHANICAL_CHECK_EVENTS_KEY] = build_mechanical_check_events(mechanical_envelopes)
            return ledger

        return ledger_cache.ledger(fingerprint, _build)

    def _page_data(
        local_usage_preview: list[ClientUsageEvent] | None = None,
        *,
        continuation_snapshot: Mapping[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
        ledger: dict[str, Any] | None = None,
    ) -> _DashboardPageData:
        """Saved-rows page context (module-level assembly with cached stores).

        ``events``/``ledger`` let the /v1 task lane assemble the page over the
        shared, cached derived ledger (kept warm by the sessions lane) instead
        of paying build_work_ledger's multi-second reduce again."""

        return build_page_data(
            store_dir,
            service=service,
            cost_ledger=cost_ledger,
            continuation_store=continuation_store,
            task_identity=task_identity,
            ingestion_health_store=ingestion_health,
            local_usage_preview=local_usage_preview,
            continuation_snapshot=continuation_snapshot,
            task_csrf_token=task_csrf_token,
            events=events,
            ledger=ledger,
        )

    @app.get("/api/control")
    def control_projection_json() -> dict[str, Any]:
        # No CSRF token or execution authority appears in this read model.
        return _control_payload()

    def _control_projection_readonly() -> ControlProjection:
        """Read existing control state without creating a store on GET."""

        if not control_store.actions_path.exists():
            return ControlProjection()
        return control_store.project()

    def _observed_control_tasks() -> list[dict[str, str]]:
        projection = _dashboard_task_projection(_page_data())
        rows: list[dict[str, str]] = []
        for task in projection.get("tasks", []):
            if not isinstance(task, Mapping):
                continue
            public_id = str(task.get("public_task_id") or "")
            if public_id:
                rows.append({"task_id": public_id, "title": _task_title(task)})
        return rows

    def _control_payload() -> dict[str, Any]:
        payload = sanitize_control_projection(
            _control_projection_readonly(),
            observed_tasks=_observed_control_tasks(),
        )
        payload["supervisor"] = {
            "state": (
                "unavailable"
                if app.state.control_supervisor_error
                else "active"
                if control_supervisor.lease.acquired
                else "standby"
            )
        }
        return payload

    @app.get("/tasks")
    def tasks_projection() -> dict[str, Any]:
        """Task list JSON served from the v1 projection."""

        # The former top-level csrf_token key is gone with the HTML form flows;
        # per-episode finding tokens remain (they identify findings to local
        # write lanes like `agentacct finding dispose`).
        return dict(_dashboard_task_projection(_page_data()))

    # ---- /v1 native-shell lane (menu bar app / SwiftBar / widgets) --------
    glance_cache = GlanceCache()

    def _require_v1_token(request: Request) -> None:
        """Bearer gate for the /v1 lane. Fails closed: a server constructed
        without a token (tests, ``agentacct api serve`` sidecars) serves 503 on
        every /v1 route rather than opening an unauthenticated lane by accident.
        Comparison is constant-time; the token comes from the discovery file."""

        if not v1_auth_token:
            raise HTTPException(
                status_code=503,
                detail=(
                    "v1 API disabled: this server was started without a v1 token. "
                    "`agentacct serve` provisions one and writes the discovery file automatically."
                ),
            )
        header = request.headers.get("authorization") or ""
        scheme, _, candidate = header.partition(" ")
        # Compare BYTES: hmac.compare_digest raises TypeError on non-ASCII str
        # (Starlette decodes header bytes as latin-1, so a garbled local client
        # can deliver one), and the reader contract needs a clean 401 — never a
        # 500 — as its "re-read the discovery file" signal.
        candidate_bytes = candidate.strip().encode("utf-8", "surrogateescape")
        expected_bytes = v1_auth_token.encode("utf-8", "surrogateescape")
        if scheme.lower() != "bearer" or not hmac.compare_digest(candidate_bytes, expected_bytes):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token for the v1 local API")

    @app.get("/v1/version")
    def v1_version(request: Request) -> dict[str, Any]:
        """Native-shell handshake: pin daemon/schema compatibility BEFORE
        parsing any payload (an incompatible daemon is a first-class UI state,
        never a JSON parse error)."""

        _require_v1_token(request)
        return {
            "schema": "agentacct.v1-version.v1",
            "version": _dashboard_importer_version(),
            "glance_schema": GLANCE_SCHEMA_VERSION,
            "sessions_schema": V1_SESSIONS_SCHEMA_VERSION,
            "session_detail_schema": V1_SESSION_DETAIL_SCHEMA_VERSION,
            "plan_schema": V1_PLAN_SCHEMA_VERSION,
            "receipt_schema": RECEIPT_SCHEMA_VERSION,
            "ingestion_schema": V1_INGESTION_SCHEMA_VERSION,
            "pid": os.getpid(),
            "store_dir": str(store_dir),
            "store_scope": store_scope,
        }

    @app.get("/v1/glance")
    def v1_glance(request: Request) -> dict[str, Any]:
        """The glance snapshot (usage · cost · plan · recent sessions).

        Fingerprint + TTL cached: a poll that finds no event change skips the
        aggregation REBUILD (the expensive part) — each poll still reads the
        event list once to compute the change key, so per-request cost is one
        store read + an O(n) hash, never a re-aggregation (see
        :mod:`agentacct.glance` for the schema contract)."""

        _require_v1_token(request)
        events = service.list_all_events()
        return glance_cache.snapshot(events, store_dir=store_dir, version=_dashboard_importer_version())

    v1_sessions_cache = V1SessionsCache()

    @app.get("/v1/sessions")
    def v1_sessions(
        request: Request,
        roots_only: bool = Query(True),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """The versioned session list for native shells (see agentacct.v1_sessions).

        ``roots_only`` filters BEFORE the recency slice (the "12 root
        sessions" fix), ``offset``/``limit`` walk the filtered population, and
        the envelope disclose every cut (totals + ``truncated``). Rows carry
        per-session weekly-plan shares with children folded into their root
        (calibrated-or-nothing). Cached like the glance: fingerprint + TTL over
        the enriched view; the per-request work is a filter + slice.
        """

        _require_v1_token(request)
        events = service.list_all_events()
        fingerprint = events_fingerprint(events)
        view = v1_sessions_cache.view(
            fingerprint,
            lambda: build_v1_sessions_view(
                _derived_work_ledger(events, fingerprint=fingerprint), events
            ),
        )
        return slice_sessions_payload(view, roots_only=roots_only, limit=limit, offset=offset)

    @app.get("/v1/session")
    def v1_session_detail(
        request: Request,
        client: str = Query(..., min_length=1),
        session_id: str = Query(..., min_length=1),
    ) -> dict[str, Any]:
        """One session's deep view: the list row + expandable steps (status,
        kind, per-step usage, attributed model lanes, machine checks) +
        descendants + the plan why-this-number block. Query params rather
        than a path segment because session ids legally contain ':' and other
        separator-shaped characters. 404 when the store has no such session —
        never an empty fabrication."""

        _require_v1_token(request)
        events = service.list_all_events()
        fingerprint = events_fingerprint(events)
        ledger = _derived_work_ledger(events, fingerprint=fingerprint)
        view = v1_sessions_cache.view(
            fingerprint,
            lambda: build_v1_sessions_view(ledger, events),
        )
        detail = build_v1_session_detail(view, ledger, client=client, session_id=session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="unknown session for this store")
        return detail

    # (fingerprint, built_at, payload) per requested day-range; bounded by the
    # route's ge/le so this can never grow past a handful of keys.
    v1_plan_cache: dict[int, tuple[int, float, dict[str, Any]]] = {}

    @app.get("/v1/plan")
    def v1_plan(
        request: Request,
        days: int = Query(30, ge=1, le=90),
    ) -> dict[str, Any]:
        """Attributed plan aggregates per plan-bearing client (calibrated-or-
        nothing): today/7d/30d window shares, a daily series aligned with the
        /usage/summary calendar buckets, a by-model split, and the unknown-time
        disclosure. The account-wide provider truth stays in glance limits[]
        — a different quantity, deliberately not duplicated here."""

        _require_v1_token(request)
        events = service.list_all_events()
        fingerprint = events_fingerprint(events)
        moment = time.time()
        cached = v1_plan_cache.get(days)
        if cached is not None and cached[0] == fingerprint and (moment - cached[1]) < 30.0:
            return cached[2]
        payload = build_v1_plan_payload(events, days=days, now=moment)
        v1_plan_cache[days] = (fingerprint, moment, payload)
        return payload

    # The decorated, evidence-attached Task projection, fingerprint + TTL
    # cached AND single-flighted: the build is an expensive full-ledger reduce,
    # so concurrent app requests (the tasks list + several receipts fired at
    # once) must share ONE build under the lock — otherwise each sync worker
    # rebuilds in parallel and they all blow the client's request timeout.
    v1_receipt_projection_cache: dict[str, tuple[int, float, dict[str, Any]]] = {}
    v1_receipt_projection_lock = threading.Lock()

    def _v1_task_projection() -> dict[str, Any]:
        # Keyed on the v1 event log. Machine checks ARE v1 events, so they
        # invalidate this the instant they land; the only writes it does not
        # notice within the TTL are evidence-store-only imports (connector /
        # capture-hook), the same bound every sibling /v1 cache already has —
        # the real fix for that is materialization, not a hotter key. Reusing
        # the shared ledger stacks that ledger's own 30s TTL under this cache's,
        # so those fingerprint-invisible inputs (cost/run-report/mechanical-obs
        # imports) can lag up to ~60s here — the SAME staleness class and the
        # SAME reused-ledger profile the sessions lane already accepts.
        events = service.list_all_events()
        fingerprint = events_fingerprint(events)
        cached = v1_receipt_projection_cache.get("projection")
        if cached is not None and cached[0] == fingerprint and (time.time() - cached[1]) < 30.0:
            return cached[2]
        with v1_receipt_projection_lock:
            # Double-check: a concurrent request may have just built it.
            cached = v1_receipt_projection_cache.get("projection")
            if cached is not None and cached[0] == fingerprint and (time.time() - cached[1]) < 30.0:
                return cached[2]
            # Assemble the projection over the SHARED derived ledger instead of
            # rebuilding the full ledger here. That ledger (WorkLedgerCache,
            # single-flighted, fingerprint + TTL cached) is the same one the
            # sessions lane keeps warm under polling, so the tasks/receipts the
            # app fires alongside it pay the multi-second reduce at most once,
            # shared — never once per request. The reduce sees the SAME inputs
            # build_page_data would have used: the run-report cap is the shared
            # _LEDGER_RUN_REPORT_LIMIT on both paths, and the cost-event order
            # difference is re-sorted away inside build_proxy_usage_events. So
            # the projection is byte-identical to the self-built one it
            # replaces — for any store, not only the MCP-first (runs/ empty)
            # case verified on the live store.
            ledger = _derived_work_ledger(events, fingerprint=fingerprint)
            projection = _dashboard_task_projection(_page_data(events=events, ledger=ledger))
            # Weekly-plan shares ride the same cached projection: deterministic
            # from the same event log, so the cache key already covers them.
            _stamp_task_plan_shares(projection, events)
            attention_tasks = _visible_tasks(projection)
            attention_tasks.sort(
                key=lambda task: float(task.get("last_activity_at") or 0.0),
                reverse=True,
            )
            projection["_dashboard_receipt_attention"] = _dashboard_receipt_attention(
                attention_tasks,
                latest_store_activity_at=latest_store_activity(attention_tasks),
            )
            v1_receipt_projection_cache["projection"] = (fingerprint, time.time(), projection)
            return projection

    def _visible_tasks(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            task
            for task in projection.get("tasks", [])
            if isinstance(task, Mapping) and str(task.get("public_task_id") or "")
        ]

    @app.get("/v1/tasks")
    def v1_tasks(
        request: Request,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """The versioned Task list: one compact Receipt summary per Task
        (the two axes + cost + activity), newest first. A Task is the
        convergence of a root session with its continuations and subagents —
        the unit a Receipt is written for. Cheap under polling (cached
        projection); the complete attention count and bounded preview are built
        once per cache refresh, while each request maps only its recent slice."""

        _require_v1_token(request)
        projection = _v1_task_projection()
        tasks = _visible_tasks(projection)
        latest = latest_store_activity(tasks)
        tasks.sort(key=lambda task: float(task.get("last_activity_at") or 0.0), reverse=True)
        total = len(tasks)
        window = tasks[offset : offset + limit]
        rows = [
            build_receipt_summary(
                task,
                public_task_id=str(task.get("public_task_id")),
                title=_task_title(task),
                latest_store_activity_at=latest,
            )
            for task in window
        ]
        return {
            "schema": RECEIPT_SCHEMA_VERSION,
            "tasks": rows,
            "total": total,
            "offset": offset,
            "limit": limit,
            "truncated": offset + limit < total,
            # Additive, exact across every visible Task, and preview-bounded.
            # Unlike ``tasks``, this is never scoped to the recent page.
            "attention": projection["_dashboard_receipt_attention"],
        }

    @app.get("/v1/receipt")
    def v1_receipt(
        request: Request,
        task: str = Query(..., min_length=1, alias="task"),
    ) -> dict[str, Any]:
        """One Task's full Receipt: the 8 questions, the two orthogonal axes,
        per-field provenance, and the gaps block. 404 when the store has no
        such Task — never an empty fabrication."""

        _require_v1_token(request)
        projection = _v1_task_projection()
        tasks = _visible_tasks(projection)
        selected = next((row for row in tasks if str(row.get("public_task_id")) == task), None)
        if selected is None:
            raise HTTPException(status_code=404, detail="unknown task for this store")
        return build_receipt(
            selected,
            public_task_id=str(selected.get("public_task_id")),
            title=_task_title(selected),
            latest_store_activity_at=latest_store_activity(tasks),
        )

    @app.post("/v1/disposition")
    def v1_disposition(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """Record ONE human attention transition for a surfaced finding or a
        recorded blocker — the first user-originated write on the /v1 lane.

        Bearer-gated like every /v1 route; the body must echo the optimistic
        ``expected_revision`` the caller displayed, so a concurrent change is
        a 409, never a silent overwrite. A resolve REQUIRES a note. The write
        appends a server-validated disposition event (see finding_disposition)
        — it never rewrites machine evidence or the agent's recorded events,
        and the response is the chain's new state, not a re-graded outcome.
        """

        _require_v1_token(request)
        kind = str(payload.get("kind") or "").strip()
        action = str(payload.get("action") or "").strip()
        raw_note = payload.get("note")
        note = raw_note.strip() if isinstance(raw_note, str) and raw_note.strip() else None
        expected_revision = payload.get("expected_revision")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise HTTPException(
                status_code=400, detail="expected_revision must be a non-negative integer"
            )
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        # The v1: namespace is the endpoint's own deterministic key space; a
        # caller-squatted key there would permanently 409 later UI actions.
        if idempotency_key.startswith("v1:"):
            raise HTTPException(
                status_code=400, detail="idempotency_key may not use the reserved v1: prefix"
            )
        try:
            if kind == "finding":
                digest = str(payload.get("target_digest") or "").strip()
                projection = _v1_task_projection()
                episode = resolve_finding_episode(projection, digest=digest)
                target = (
                    episode.get("failure_event")
                    if isinstance(episode.get("failure_event"), dict)
                    else None
                )
                if target is None:
                    raise FindingDispositionNotFound("finding target is unavailable")
                assignment_context = (
                    episode.get("assignment_context")
                    if isinstance(episode.get("assignment_context"), dict)
                    else {}
                )
                task_scope = (
                    assignment_context.get("task_scope")
                    if isinstance(assignment_context.get("task_scope"), dict)
                    else None
                )
                full_digest = finding_target_digest(target)
                if not idempotency_key:
                    idempotency_key = f"v1:finding:{full_digest}:{expected_revision}:{action}"
                recorded = service.record_finding_disposition(
                    target_event=target,
                    action=action,
                    expected_revision=expected_revision,
                    note=note,
                    idempotency_key=idempotency_key,
                    task_scope=task_scope,
                    transport="v1",
                )
            elif kind == "blocker":
                blocked_event_id = str(payload.get("blocked_event_id") or "").strip()
                if not blocked_event_id:
                    raise HTTPException(status_code=400, detail="blocked_event_id is required")
                # Same quarantine as the finding lane: only a blocker some read
                # surface actually SHOWS is disposable — the id must be a
                # surfaced work item's current blocked event.
                projection = _v1_task_projection()
                surfaced_blocked_ids = {
                    str(item.get("current_blocked_event_id") or "")
                    for task in projection.get("tasks", [])
                    if isinstance(task, Mapping)
                    for item in (task.get("work_items") or [])
                    if isinstance(item, Mapping)
                }
                if blocked_event_id not in surfaced_blocked_ids:
                    raise FindingDispositionNotFound(
                        "no surfaced blocker matches that event id"
                    )
                events = service.list_all_events()
                target = next(
                    (
                        event
                        for event in events
                        if str(event.get("event_id") or "") == blocked_event_id
                    ),
                    None,
                )
                if target is None:
                    raise FindingDispositionNotFound(
                        "blocker target event is not in this ledger"
                    )
                if not idempotency_key:
                    idempotency_key = f"v1:blocker:{blocked_event_id}:{expected_revision}:{action}"
                recorded = service.record_blocker_disposition(
                    target_event=target,
                    action=action,
                    expected_revision=expected_revision,
                    note=note,
                    idempotency_key=idempotency_key,
                    transport="v1",
                )
            else:
                raise HTTPException(status_code=400, detail="kind must be finding or blocker")
        except FindingDispositionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FindingDispositionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        metadata = (
            recorded.get("metadata") if isinstance(recorded.get("metadata"), Mapping) else {}
        )
        return {
            "ok": True,
            "kind": kind,
            "action": action,
            "state": metadata.get("next_state"),
            "revision": metadata.get("revision"),
            "event_id": recorded.get("event_id"),
        }

    @app.get("/v1/ingestion")
    def v1_ingestion(request: Request) -> dict[str, Any]:
        """Source/ingestion health for the native shell's Sources surface —
        the bearer-gated twin of the legacy ``/ingestion/health`` route. The
        snapshot is passed through verbatim (same additive contract): per-source
        state and recency, watcher liveness, and actionable issues. The shell
        renders exactly what the snapshot vouches for; a source the importer
        has never seen simply is not listed — absence stays absence."""

        _require_v1_token(request)
        return {
            "schema": V1_INGESTION_SCHEMA_VERSION,
            "ingestion": ingestion_health.snapshot(),
        }

    @app.get("/")
    def index() -> dict[str, Any]:
        """A machine-readable front door (the HTML dashboard is retired).

        Everything a human wants lives in `agentacct tui`; this route exists so
        a pasted server URL answers with orientation instead of a bare 404."""

        return {
            "service": "agentacct-local-api",
            "ui": "run `agentacct tui` in a terminal (the HTML dashboard was retired)",
            "endpoints": [
                "/health",
                "/ingestion/health",
                "/overview",
                "/timeline",
                "/sessions",
                "/attention",
                "/work-items",
                "/usage/summary",
                "/tasks",
                "/api/control",
                "/evidence/status",
                "/v1/version (bearer token from the store's local-api.json)",
                "/v1/glance (bearer token from the store's local-api.json)",
                "/v1/sessions (bearer token from the store's local-api.json)",
                "/v1/session?client=&session_id= (bearer token from the store's local-api.json)",
                "/v1/plan (bearer token from the store's local-api.json)",
                "/v1/tasks (bearer token from the store's local-api.json)",
                "/v1/receipt?task= (bearer token from the store's local-api.json)",
                "/v1/ingestion (bearer token from the store's local-api.json)",
            ],
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        # Health-check consumers (activation readiness + the read canary) match
        # this service string. It carries the agentacct brand now; the pre-rename
        # "agent-sentinel-local-api" stays ACCEPTED by both recognizers forever,
        # so a running old dashboard checked by newer code is still recognized.
        sync_health = ingestion_health.snapshot()
        return {
            "ok": True,
            "service": "agentacct-local-api",
            "ingestion_status": sync_health["state"],
            "ingestion": sync_health,
        }

    @app.get("/ingestion/health")
    def ingestion_health_status() -> dict[str, Any]:
        return ingestion_health.snapshot()

    @app.get("/overview")
    def overview() -> dict[str, Any]:
        """Derived work-ledger overview (schema agent-sentinel.work-ledger.v2).

        Additive since v1: fresh/cache_creation/cache_read token triples next
        to every total_tokens figure (total_tokens keeps its everything-
        included meaning), ambiguous_usage_count,
        context_matched_unallocated_(usage_)count,
        usage_without_mcp_context_count, attention_item_count,
        attention_counts, attention_group_count. The schema_version envelope
        key is new; no existing keys were removed.
        """
        ledger = _derived_work_ledger()
        return {"schema_version": ledger["schema_version"], "overview": ledger["overview"]}

    @app.get("/timeline")
    def timeline(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        """Derived usage/work/evidence timeline (schema agent-sentinel.work-ledger.v2).

        Additive since v1: event_kind "usage_diagnostic", join strategies
        "diagnostic_not_usage_truth" and "parent_child_context_hint",
        tokens_fresh/tokens_cache_read/tokens_cache_creation on usage-bearing
        entries, and a client key on usage/proxy/diagnostic entries. Rows are
        row-level and unchanged by the dashboard's grouped display view. The
        schema_version envelope key is new; no existing keys were removed.
        """
        ledger = _derived_work_ledger()
        return {"schema_version": ledger["schema_version"], "timeline": ledger["timeline"][:limit]}

    @app.get("/work-items")
    def work_items(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        """Derived MCP work items (schema agent-sentinel.work-ledger.v2).

        Since v1 (breaking, part of why the version bumped): work_id is
        namespaced client::session::section_id (raw section_id still present).
        Additive: join_explanation, usage_fresh_total /
        usage_cache_read_total / usage_cache_creation_total. The
        schema_version envelope key is new; no existing keys were removed.
        """
        ledger = _derived_work_ledger()
        return {"schema_version": ledger["schema_version"], "work_items": ledger["work_items"][:limit]}

    @app.get("/work-items/{work_id}")
    def work_item(work_id: str) -> dict[str, Any]:
        """One work item by namespaced work_id (raw section_id fallback kept)."""
        ledger = _derived_work_ledger()
        for item in ledger["work_items"]:
            if item.get("work_id") == work_id or item.get("section_id") == work_id:
                return {"schema_version": ledger["schema_version"], "work_item": item}
        raise HTTPException(status_code=404, detail=f"work item not found: {work_id}")

    @app.get("/sessions")
    def sessions(
        request: Request,
        limit: int = Query(200, ge=1, le=1000),
        client: str = "all",
        project: str = "all",
        join: str = "all",
        kind: str = "grouped",
        days: str = "30",
        sort: str = "recent",
        work: str = "all",
        show: int = Query(SESSION_ROLLUP_DISPLAY_LIMIT, ge=1, le=1000),
    ) -> Any:
        """Session rollup: one entry per (client, base client session id).

        JSON-only since the HTML retirement (the Sessions explorer page is
        gone; ``agentacct tui`` is the interactive surface). The legacy HTML
        filter params (``client``/``project``/``join``/``kind``/``days``/
        ``sort``/``work``/``show``) are still ACCEPTED for wire compatibility
        and still ignored with the same additive honesty: any of them riding
        a request adds the ``ignored_html_params`` list naming them, so a
        script can never mistake the unfiltered rollup for a filtered one. Additive rollup fields (schema
        stays v1): entry ``duration_seconds`` (own first/last activity span,
        None unless honestly derivable) and ``usage.turns_total``
        (importer-recorded turn counts summed over the entry's OWN rows only,
        None when no row carries one; children never merged).

        Serves ledger["session_rollup"] verbatim — the rollup is built once in
        work_ledger.build_session_rollup and NEVER regrouped here (the
        one-matcher lesson). ``limit`` slices sessions[] only; total_sessions
        and summary always describe the full store. Entries keep FULL session
        ids: this is the machine-local JSON surface (same precedent as
        /work-items work_id); HTML consumers must render
        client_session_id_short. GET-only, zero writes.

        Additive (Phase 2.6): entry ``project_source`` ("claude_worktree"
        when the project label was remapped from a temporary Claude worktree
        path to the owning repo) and ``join.context_scope`` ("this_store" |
        "other_project" — an other_project session's MCP context, if any,
        lives in that project's own store). "other_project" fires only when
        THIS store uses the conventional per-project layout (explicit
        store_scope), the session is unjoined with zero sections and zero
        conflict-vetoed rows here, and both project labels are known,
        non-home, and differ case-insensitively; every ambiguity stays
        "this_store".

        Additive (instrumentation markers): entry ``instrumentation_state``
        ("pre_instrumentation" | "post_instrumentation" | "unknown"),
        ``instrumentation_state_basis`` ("session_start_vs_marker" |
        "inherited_from_root" | None; "unknown" always carries basis None)
        and ``instrumentation_installed_at`` (the client's earliest
        CLI-authored marker time, or None); summary ``instrumentation`` block
        (markers_by_client, invalid_marker_count — marker-typed events that
        failed provenance/plausibility validation, counted, never silent —
        pre/post/unknown session counts, post_context_kpi). Schema stays v1 —
        nothing existing changed.
        """
        ignored = sorted(
            name
            for name in ("client", "project", "join", "kind", "days", "sort", "work", "show")
            if name in request.query_params
        )
        ledger = _derived_work_ledger()
        rollup = ledger["session_rollup"]
        rollup_sessions = rollup.get("sessions") if isinstance(rollup, dict) else []
        rollup_sessions = rollup_sessions if isinstance(rollup_sessions, list) else []
        payload: dict[str, Any] = {
            "schema_version": ledger["schema_version"],
            "session_rollup_schema_version": rollup.get("schema_version") if isinstance(rollup, dict) else None,
            "total_sessions": len(rollup_sessions),
            "summary": (rollup.get("summary") if isinstance(rollup, dict) else None) or {},
            "sessions": rollup_sessions[:limit],
        }
        if ignored:
            # Additive wire honesty: these params filter the HTML page only.
            payload["ignored_html_params"] = ignored
        return payload

    @app.get("/attention")
    def attention() -> dict[str, Any]:
        """Grouped attention items: one group per cause, count + bounded
        redacted example refs, served verbatim from ledger["attention_groups"]
        (groups derive FROM the detail items, so total_items can never
        disagree with the raw attention list). GET-only, zero writes.
        """
        ledger = _derived_work_ledger()
        groups_payload = ledger["attention_groups"]
        groups = groups_payload.get("groups") if isinstance(groups_payload, dict) else []
        groups = groups if isinstance(groups, list) else []
        total_items = int((groups_payload.get("total_items") if isinstance(groups_payload, dict) else 0) or 0)
        return {
            "schema_version": ledger["schema_version"],
            "total_items": total_items,
            "attention_groups": groups,
        }

    @app.get("/evidence/status")
    def evidence_status() -> dict[str, Any]:
        return service.evidence.status()

    @app.get("/evidence/events")
    def evidence_events(
        limit: int = Query(50, ge=1, le=100),
        cursor: int | None = Query(None, ge=1),
        source_type: str | None = None,
        source_system: str | None = None,
        dimension: str | None = None,
        assertion: str | None = None,
    ) -> dict[str, Any]:
        if assertion not in {None, "observed", "claimed"}:
            raise HTTPException(status_code=422, detail="assertion must be observed or claimed")
        records = service.evidence.records(
            limit=limit + 1,
            order_by="arrival",
            descending=True,
            arrival_before_sequence=cursor,
            source_type=source_type,
            source_system=source_system,
            dimension=dimension,
            assertion=assertion,
        )
        has_more = len(records) > limit
        visible_records = records[:limit]
        next_cursor = visible_records[-1].first_receipt_sequence if has_more and visible_records else None
        evidence = [
            {
                "envelope": record.envelope.to_dict(),
                "first_receipt_sequence": record.first_receipt_sequence,
                "last_receipt_sequence": record.last_receipt_sequence,
                "receipt_count": record.receipt_count,
                "duplicate_receipt_count": record.duplicate_receipt_count,
                "is_conflict": record.is_conflict,
            }
            for record in visible_records
        ]
        return {
            "count": len(evidence),
            "evidence": evidence,
            "page": {
                "limit": limit,
                "returned": len(evidence),
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
        }

    @app.get("/evidence/events/{evidence_id}")
    def evidence_event(evidence_id: str) -> dict[str, Any]:
        if not service.evidence.enabled:
            raise HTTPException(status_code=404, detail="Evidence v2 is disabled")
        envelope = service.evidence.store.get(evidence_id)
        if envelope is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        return {"envelope": envelope.to_dict()}

    @app.get("/evidence/claimed-links")
    def evidence_claimed_links(
        limit: int = Query(100, ge=1, le=1000),
        validation_state: str | None = None,
    ) -> dict[str, Any]:
        if not service.evidence.enabled:
            return {"claimed_links": []}
        if validation_state not in {None, "pending", "valid", "invalid"}:
            raise HTTPException(status_code=422, detail="unsupported claimed-link validation state")
        records = service.evidence.store.query_claimed_links(
            limit=limit,
            validation_state=validation_state,
        )
        return {
            "claimed_links": [
                {
                    "link": record.link.to_dict(),
                    "validation_state": record.validation_state,
                    "is_conflict": record.is_conflict,
                    "receipt_count": record.receipt_count,
                    "duplicate_receipt_count": record.duplicate_receipt_count,
                }
                for record in records
            ]
        }

    @app.get("/evidence/product")
    def evidence_product(limit: int = Query(10_000, ge=1, le=10_000)) -> dict[str, Any]:
        return service.evidence.product(limit=limit)

    @app.get("/evidence/work-graph")
    def evidence_work_graph(limit: int = Query(10_000, ge=1, le=10_000)) -> dict[str, Any]:
        product = service.evidence.product(limit=limit)
        return {"schema_version": product["schema_version"], "summary": product["summary"], "work_graph": product["work_graph"]}

    @app.get("/evidence/matrix")
    def evidence_matrix(limit: int = Query(10_000, ge=1, le=10_000)) -> dict[str, Any]:
        product = service.evidence.product(limit=limit)
        return {"schema_version": product["schema_version"], "summary": product["summary"], "evidence_matrix": product["evidence_matrix"]}

    @app.get("/evidence/discrepancies")
    def evidence_discrepancies(limit: int = Query(10_000, ge=1, le=10_000)) -> dict[str, Any]:
        product = service.evidence.product(limit=limit)
        return {"schema_version": product["schema_version"], "summary": product["summary"], "discrepancies": product["discrepancies"]}

    @app.get("/evidence/cost-outcome-basis")
    def evidence_cost_outcome_basis(limit: int = Query(10_000, ge=1, le=10_000)) -> dict[str, Any]:
        product = service.evidence.product(limit=limit)
        return {"schema_version": product["schema_version"], "summary": product["summary"], "cost_outcome_basis": product["cost_outcome_basis"]}

    @app.post("/evidence/replay-v1")
    def evidence_replay_v1() -> dict[str, Any]:
        return service.evidence.replay_v1(service.list_all_events(), transport="internal")

    @app.post("/work-events")
    def record_work_event(request: WorkEventRecordRequest) -> dict[str, Any]:
        event_type = {
            "section": f"section_{request.status}",
            "task": f"task_{request.status}",
            "machine_check": "machine_check",
            "client_context": "client_context_attached",
            "usage_debug": "agent_usage_debug_reported",
            "note": "note",
            "event": "work_event",
        }[request.event_kind]
        work_event = WorkEvent(
            event_kind=request.event_kind,
            source=request.source,
            transport="http",
            status=request.status,
            occurred_at=request.occurred_at if request.occurred_at is not None else time.time(),
            source_event_id=request.source_event_id,
            run_id=request.run_id,
            work_id=request.work_id,
            section_id=request.section_id,
            title=request.title,
            objective=request.objective,
            summary=request.summary,
            blocker=request.blocker,
            next_step=request.next_step,
            client=request.client,
            client_session_id=request.client_session_id,
            client_transcript_id=request.client_transcript_id,
            parent_client_session_id=request.parent_client_session_id,
            turn_id=request.turn_id,
            message_id=request.message_id,
            request_id=request.request_id,
            files=tuple(request.files),
            original_event_type=event_type,
        )
        recorded = service.record_event(work_event.to_v1_event(), transport="http")
        return {
            "work_event": WorkEvent.from_v1_event(recorded, transport="http").to_dict(),
            "v1_event": recorded,
        }

    async def _bounded_raw_body(request: Request, *, maximum_bytes: int) -> bytes:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > maximum_bytes:
                    raise HTTPException(status_code=413, detail="capture payload is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content-length") from exc
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > maximum_bytes:
                raise HTTPException(status_code=413, detail="capture payload is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    @app.get("/capture/capabilities")
    def capture_capabilities(vendor: str | None = None) -> dict[str, Any]:
        capabilities = DEFAULT_CAPTURE_REGISTRY.capabilities(vendor)
        if vendor is not None:
            if capabilities is None or isinstance(capabilities, dict):
                raise HTTPException(status_code=404, detail="unsupported capture vendor")
            rows = {capabilities.vendor: capabilities.to_dict()}
        else:
            assert isinstance(capabilities, dict)
            rows = {name: capability.to_dict() for name, capability in capabilities.items()}
        return {
            "capabilities": rows,
            "privacy": {
                "capture_mode": "metadata_only",
                "captures_usage": False,
                "captures_cost": False,
            },
        }

    @app.get("/capabilities/agents")
    def agent_capabilities() -> dict[str, Any]:
        return agent_capability_manifest()

    @app.get("/capture/manifests/{vendor}")
    def capture_manifest(vendor: str) -> dict[str, Any]:
        try:
            rendered = render_hook_manifest(vendor)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "vendor": rendered.vendor,
            "relative_path": rendered.relative_path,
            "content": rendered.to_dict(),
            "written": False,
            "activation": "opt_in",
        }

    @app.post("/capture/{vendor}/{host_event}")
    async def capture_host_event(vendor: str, host_event: str, request: Request) -> dict[str, Any]:
        """Bounded, metadata-only local hook receiver; input is never echoed."""

        payload = await _bounded_raw_body(request, maximum_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
        return capture_hook_payload(
            service.evidence,
            vendor=vendor,
            host_event=host_event,
            payload=payload,
            context=CaptureContext(host_event=host_event),
        )

    @app.post("/control/evaluate")
    def evaluate_control(request: ControlSignalRequest) -> dict[str, Any]:
        signal = ControlSignal(
            action=request.action,  # type: ignore[arg-type]
            target_type=request.target_type,
            target_id=request.target_id,
            recommendation=request.recommendation,
            requested_mode=request.requested_mode,  # type: ignore[arg-type]
            evidence_basis=request.evidence_basis,
            cost_confidence=request.cost_confidence,
            supporting_evidence_ids=tuple(request.supporting_evidence_ids),
            explicit_conservative_approval=request.explicit_conservative_approval,
            controller_owns_execution=request.controller_owns_execution,
            conflicting=request.conflicting,
            expires_at=request.expires_at,
            idempotency_key=request.idempotency_key,
        )
        validation = validate_supporting_evidence(signal, service.evidence)
        decision = evaluate_control_signal(signal, supporting_evidence=validation)
        return {
            "signal_id": decision.signal_id,
            "requested_mode": decision.requested_mode,
            "effective_mode": decision.effective_mode,
            "hard_enforcement_allowed": decision.hard_enforcement_allowed,
            "reason": decision.reason,
            "supporting_evidence_validation": validation.to_dict(),
            "external_action_dispatched": False,
        }

    @app.get("/events")
    def list_events(limit: int = Query(50, ge=1, le=200), run_id: str | None = None) -> dict[str, Any]:
        if run_id is not None:
            try:
                validate_run_id(run_id)
            except ValueError as exc:
                raise _invalid(exc) from exc
        return {"events": service.list_events(limit=limit, run_id=run_id)}

    @app.get("/events/summary")
    def event_summary(limit: int = Query(200, ge=1, le=200), run_id: str | None = None) -> dict[str, Any]:
        if run_id is not None:
            try:
                validate_run_id(run_id)
            except ValueError as exc:
                raise _invalid(exc) from exc
        return {"summary": service.summarize_events(limit=limit, run_id=run_id)}

    @app.get("/usage/sources")
    def usage_sources() -> dict[str, Any]:
        return {"sources": [source.to_dict() for source in _discover_local_usage_sources(usage_discovery)]}

    @app.get("/usage/preview")
    def usage_preview(limit_sessions: int = Query(DASHBOARD_USAGE_LIMIT_SESSIONS, ge=1, le=500)) -> dict[str, Any]:
        preview = _discover_local_usage(usage_discovery, limit_sessions=limit_sessions)
        return {"totals": _usage_event_totals(preview), "events": [event.to_sentinel_event() for event in preview]}

    @app.get("/usage/summary")
    def usage_summary(
        client: str = "all",
        model: str = "all",
        days: str = "30",
        granularity: str = "auto",
    ) -> dict[str, Any]:
        """Usage cube JSON (schema agent-sentinel.usage-summary.v1, PRD §5.4):
        tokens by platform / model / period over SAVED usage rows only — the
        same trusted-import intake every dashboard surface shares, so
        diagnostic events and shadowed legacy rows never enter, and no live
        scan runs. JSON parity with the /tokens explorer (the chart's
        per-period platform split rides in by_period[].by_client).

        ``client``/``days``/``granularity`` are whitelisted → 422 on unknown
        values. ``model`` is echoed and validated against models present in
        saved rows: an unknown model returns the EMPTY result with the filter
        echoed (filters_echo.model_matches_saved_rows false) — never a guess
        (locked decision). ``granularity=auto`` applies the locked range rule
        (daily for 7/30, weekly for 90/all); filters_echo carries both the
        requested and the effective value. GET-only, zero writes.

        Bounded ranges also return ``range_context.history_outside_range`` so
        a client with saved older history is not mistaken for deleted data;
        ``by_client`` itself remains strictly scoped to the requested range.
        """

        if client not in {"all", *KNOWN_USAGE_CLIENTS}:
            raise HTTPException(status_code=422, detail=f"unknown client filter: {client}")
        if days not in USAGE_CUBE_DAYS_CHOICES:
            raise HTTPException(status_code=422, detail=f"unknown days filter: {days}")
        if granularity not in {"auto", *USAGE_CUBE_GRANULARITY_CHOICES}:
            raise HTTPException(status_code=422, detail=f"unknown granularity: {granularity}")
        usage_view = _build_usage_view([], service.list_all_events())
        records = usage_view.saved_records
        cube_records = [*records, *usage_view.excluded_saved_records]
        effective_granularity = resolve_granularity(days, granularity)
        today = date.today()
        cube = build_usage_cube(
            cube_records,
            record_time=_usage_record_time,
            client=None if client == "all" else client,
            model=None if model == "all" else model,
            days=days_choice_to_int(days),
            granularity=effective_granularity,
            # ONE today per request (same rule as the HTML pages).
            today=today,
        )
        all_time_cube = (
            build_usage_cube(
                cube_records,
                record_time=_usage_record_time,
                client=None if client == "all" else client,
                model=None if model == "all" else model,
                days=None,
                # Only by_client is consumed for range context. Weekly keeps
                # a long-lived all-time store bounded without changing those
                # client totals.
                granularity="weekly",
                today=today,
            )
            if days != "all"
            else None
        )
        history_outside_range = (
            _usage_history_outside_range(
                current_cube=cube,
                all_time_cube=all_time_cube,
                records=cube_records,
                model=None if model == "all" else model,
                days=days_choice_to_int(days),
                today=today,
            )
            if all_time_cube is not None
            else []
        )
        excluded_records, excluded_unknown_time_rows = filter_usage_records(
            usage_view.excluded_saved_records,
            record_time=_usage_record_time,
            client=None if client == "all" else client,
            model=None if model == "all" else model,
            days=days_choice_to_int(days),
            today=today,
        )
        payload = {
            "schema_version": USAGE_SUMMARY_SCHEMA_VERSION,
            "filters_echo": {
                "client": client,
                "model": model,
                "days": days,
                "granularity": effective_granularity,
                "granularity_requested": granularity,
                "model_matches_saved_rows": model == "all" or model in models_in_records(
                    [*records, *usage_view.excluded_saved_records]
                ),
            },
            "totals": cube["totals"],
            "usage_exclusions": {
                "non_additive_rows": len(excluded_records),
                "unknown_time_rows": excluded_unknown_time_rows,
                "reason": _usage_exclusion_reason(excluded_records),
                "raw_evidence_preserved": True,
            },
            "range_context": {
                "history_outside_range": history_outside_range,
            },
            "by_client": cube["by_client"],
            "by_model": cube["by_model"],
            "by_period": cube["by_period"],
        }
        return payload

    @app.post("/events")
    def record_event(request: EventRecordRequest) -> dict[str, Any]:
        return {"event": service.record_event(request.model_dump(), transport="http")}

    @app.get("/runs")
    def list_runs(limit: int = Query(50, ge=1, le=100)) -> dict[str, Any]:
        return {"runs": service.list_runs(limit=limit)}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            return service.get_run(run_id)
        except FileNotFoundError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise _invalid(exc) from exc

    @app.get("/runs/{run_id}/report")
    def get_report(run_id: str) -> dict[str, Any]:
        try:
            return service.get_report(run_id)
        except FileNotFoundError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise _invalid(exc) from exc

    @app.post("/runs/{run_id}/outcome/machine-check")
    def record_machine_check(run_id: str, request: MachineCheckRequest) -> dict[str, Any]:
        try:
            outcome = service.record_machine_check(
                run_id,
                name=request.name,
                before_exit_code=request.before_exit_code,
                after_exit_code=request.after_exit_code,
                before_summary=request.before_summary,
                after_summary=request.after_summary,
            )
            return {"outcome": outcome}
        except FileNotFoundError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise _invalid(exc) from exc

    return app
