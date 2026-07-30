from __future__ import annotations

import hashlib
import base64
import hmac
import html
import json
import math
import os
import secrets
import threading
import time
# ``field`` is aliased: several helpers below use ``field`` as a loop variable.
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, quote, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .activation import ActivationStateStore, build_activation_snapshot
from .agent_capabilities import agent_capability_manifest
from .client_usage import (
    SUPPORTED_CLIENTS,
    ClientUsageDiscoveryResult,
    ClientUsageEvent,
    apply_pricing_estimate_to_event,
    bind_discovered_usage_source_namespaces,
    build_usage_import_diagnostics,
    build_usage_import_write_batches,
    classify_usage_write_conflict_candidates,
    codex_parse_cache_scope,
    complete_session_observation_reconciliation_clients,
    _emit_scan_phase,
    usage_progress_scope,
    discover_client_usage_with_diagnostics,
    is_local_usage_import_event,
    plan_local_usage_import,
    promote_unknown_cost_reprices,
    source_namespace_adoption_candidates,
    usage_less_session_observations,
)
from .canonical_day_cube import (
    CANONICAL_USAGE_SUMMARY_SCHEMA_VERSION,
    build_canonical_day_cube,
)
from .canonical_read import (
    FIRST_DATED_DAY,
    LAST_DATED_DAY,
    CanonicalReadUnavailable,
    CanonicalSessionListRead,
    CanonicalTaskDetailRead,
    CanonicalTaskListRead,
    CanonicalUsageDayRead,
)
from .capture import CaptureContext, DEFAULT_CAPTURE_REGISTRY, render_hook_manifest
from .capture.registry import DEFAULT_MAX_PAYLOAD_BYTES
from .capture_runtime import capture_hook_payload
from .context_bridge import build_usage_context_bridge
from .connector_runtime import import_connector_records
from .connectors import (
    ConnectorError,
    ControlSignal,
    EntireGitConnector,
    OpenLITOTLPConnector,
    PaperclipSnapshotConnector,
    evaluate_control_signal,
)
from .connectors.control import normalize_supporting_evidence_ids, validate_supporting_evidence
from .control_plane import (
    ControlPlaneError,
    ControlProjection,
    ControlStore,
    IdempotencyConflict,
    InvalidTransition,
    RecordNotFound,
    RevisionConflict,
    contract_requires_launch_approval,
)
from .control_web import render_control_body, sanitize_control_projection
from .cost import PRICING_CATALOG_PATH_ENV, CostLedger, estimate_model_cost_breakdown_usd, has_model_price, pricing_catalog, pricing_catalog_path_for_store, pricing_catalog_scope, reset_pricing_catalog_cache
from .evidence_html import (
    render_advanced_index_body,
    render_cost_outcome_basis_body,
    render_discrepancies_body,
    render_evidence_matrix_body,
    render_work_graph_body,
)
from .evidence_store import EVIDENCE_STORE_DIRNAME
from .finding_disposition import (
    FindingDispositionConflict,
    FindingDispositionNotFound,
    disposition_for_event,
    finding_target_digest,
    reduce_finding_dispositions,
)
from .pricing_catalog import ensure_fresh_pricing_snapshot
from .env_compat import read_env_alias
from .localhost_guard import install_localhost_guard
from .ingestion_health import (
    EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE,
    IngestionHealthStore,
    apply_evidence_refreshable_usage_health,
    health_scan_results,
    importer_build_id,
    session_observation_conflict_error_code,
)
from .join_rules import namespace_join_compatible
from .mechanical_checks import build_mechanical_check_events
from .service import SentinelService, SessionObservationConflict
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
from .supervisor import OwnedSupervisor, SupervisorAlreadyRunning, SupervisorError
from .task_continuations import ClientSessionRef, ContinuationTaskError, ContinuationTaskStore
from .task_identity import TaskIdentityCodec
from .task_intelligence import build_task_intelligence
from .task_outcome import (
    evidence_event_key,
    finding_check_key,
    latest_check_events,
    reduce_task_outcome,
)
from .task_projection import build_task_projection
from .usage_cube import (
    KNOWN_USAGE_CLIENTS,
    UNKNOWN_PERIOD,
    USAGE_CUBE_DAYS_CHOICES,
    USAGE_CUBE_GRANULARITY_CHOICES,
    USAGE_SUMMARY_SCHEMA_VERSION,
    build_usage_cube,
    client_lane_class,
    days_choice_to_int,
    dominant_cost_confidence,
    filter_usage_records,
    models_in_records,
    normalized_cost_confidence,
    normalized_usage_confidence,
    resolve_granularity,
    usage_bucket_date,
    week_start,
)
from .log_evidence import summarize_refused_recording_attempts
from .usage_truth import (
    CODEX_REPLAY_QUARANTINE_STATE,
    is_diagnostic_event,
    local_usage_additivity,
    local_usage_source_namespace_ambiguous_identities,
    normalized_local_usage_session_id,
    recognized_local_usage_row_identity,
    selected_local_session_observation_source_identities,
    split_shadowed_legacy_usage_events,
)
from .work_ledger import (
    _base_session_display_label,
    _project_identity,
    _project_label_info,
    _safe_project_label,
    build_work_ledger,
    capped_rows,
)
from .work_events import WORK_EVENT_KINDS, WORK_EVENT_STATUSES, WorkEvent

DASHBOARD_USAGE_LIMIT_SESSIONS = 500
# Recent-activity feed on the overview shows a newest-first slice; the full
# history lives on /sessions.
DASHBOARD_RECENT_ACTIVITY_LIMIT = 25
# The pinned Needs-attention strip shows the newest open findings/blockers; the
# rest stay one click away so the strip can never bury the recent-activity feed.
DASHBOARD_ATTENTION_LIMIT = 6
MECHANICAL_PROJECTION_LIMIT = 10_000
ADVANCED_EVIDENCE_PRODUCT_LIMIT = 2_000
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


class JudgePrepareRequest(BaseModel):
    task_goal: str = "Evaluate this run's deliverable quality."
    rubric: str = "Score whether the run produced a useful, relevant, low-risk deliverable for the task goal."
    write_package: bool = True


class ValueComputeRequest(BaseModel):
    budget_usd: float | None = Field(default=None, gt=0)


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


class EntireImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=2000)
    max_commits: int = Field(default=100, ge=1, le=1000)


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


class TaskLinkRequest(BaseModel):
    """User-confirmed continuation relationship between two root chats."""

    model_config = ConfigDict(extra="forbid")

    client: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[^\r\n\x00]+$")
    client_session_id: str | None = Field(
        default=None, min_length=1, max_length=240, pattern=r"^[^\r\n\x00]+$"
    )
    target_client: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[^\r\n\x00]+$")
    target_client_session_id: str | None = Field(
        default=None, min_length=1, max_length=240, pattern=r"^[^\r\n\x00]+$"
    )
    session_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    target_session_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    confirm_cross_scope: bool = False

    @model_validator(mode="after")
    def validate_session_selectors(self) -> "TaskLinkRequest":
        raw_source = bool(self.client and self.client_session_id)
        raw_target = bool(self.target_client and self.target_client_session_id)
        token_source = self.session_token is not None
        token_target = self.target_session_token is not None
        if raw_source != raw_target or token_source != token_target or raw_source == token_source:
            raise ValueError("provide either both raw session identities or both opaque session tokens")
        return self


class TaskUnlinkRequest(BaseModel):
    """Remove one root chat from its explicit continuation Task."""

    model_config = ConfigDict(extra="forbid")

    client: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[^\r\n\x00]+$")
    client_session_id: str | None = Field(
        default=None, min_length=1, max_length=240, pattern=r"^[^\r\n\x00]+$"
    )
    session_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def validate_session_selector(self) -> "TaskUnlinkRequest":
        raw = bool(self.client and self.client_session_id)
        token = self.session_token is not None
        if raw == token:
            raise ValueError("provide either a raw session identity or an opaque session token")
        return self


class TaskRenameRequest(BaseModel):
    """Set or clear the product title override for an explicit Task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    title: str = Field(default="", max_length=160, pattern=r"^[^\r\n\x00]*$")


class FindingDispositionRequest(BaseModel):
    """One bounded, opaque, attention-only finding action."""

    model_config = ConfigDict(extra="forbid")

    # Shape validation belongs to the opaque resolver so malformed and stale
    # tokens share one generic 404 instead of exposing token internals.
    finding_token: str = Field(min_length=1, max_length=128, pattern=r"^[^\r\n\x00]+$")
    action: str = Field(pattern=r"^(?:mark_reviewed|resolve|reopen)$")
    expected_revision: int = Field(ge=0)
    note: str = Field(default="", max_length=1200, pattern=r"^[^\r\n\x00]*$")

    @field_validator("expected_revision", mode="before")
    @classmethod
    def validate_expected_revision(cls, value: Any) -> Any:
        """Accept canonical form digits without letting JSON bool/float masquerade as revisions."""

        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError("finding revision must be a non-negative integer")
        if isinstance(value, str):
            if not value or not value.isascii() or not value.isdecimal():
                raise ValueError("finding revision must be a non-negative integer")
            return int(value)
        if not isinstance(value, int):
            raise ValueError("finding revision must be a non-negative integer")
        return value

    @model_validator(mode="after")
    def validate_resolution_note(self) -> "FindingDispositionRequest":
        if self.action == "resolve" and not self.note.strip():
            raise ValueError("resolving a finding requires a note")
        return self


class ControlTaskCreateRequest(BaseModel):
    """A bounded web form for one explicit agentacct-owned Task contract."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=240, pattern=r"^[^\r\n\x00]+$")
    task_id: str = Field(default="", pattern=r"^(?:|task_[0-9a-f]{32})$")
    objective: str = Field(min_length=1, max_length=4000, pattern=r"^[^\x00]+$")
    workspace_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    mutation_mode: str = Field(default="read_only", pattern=r"^(?:read_only|workspace_write)$")
    success_checks: str = Field(default="", max_length=4000, pattern=r"^[^\x00]*$")
    budget_policy_id: str = Field(default="", pattern=r"^(?:|[A-Za-z][A-Za-z0-9_-]{0,127})$")


class ControlAttemptActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=240, pattern=r"^[^\r\n\x00]+$")
    expected_revision: int = Field(ge=1)


class ControlApprovalDecisionRequest(ControlAttemptActionRequest):
    decision: str = Field(pattern=r"^(?:approve|reject)$")


async def _read_action_request(
    request: Request,
    model: type[BaseModel],
    *,
    expected_csrf_token: str,
) -> BaseModel:
    """Validate a small JSON or urlencoded dashboard action body.

    Starlette's form parser requires the optional ``python-multipart``
    package even for simple forms. agentacct does not otherwise need that
    dependency, so these bounded text-only forms parse urlencoding directly.
    """

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > 16_384:
                raise HTTPException(status_code=413, detail="task action body must be <= 16384 bytes")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    body = await request.body()
    if len(body) > 16_384:
        raise HTTPException(status_code=413, detail="task action body must be <= 16384 bytes")
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate task action field: {key}")
            result[key] = value
        return result

    try:
        if content_type == "application/json":
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
        elif content_type in {"", "application/x-www-form-urlencoded"}:
            payload = unique_object(
                parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
            )
        else:
            raise HTTPException(status_code=415, detail="use application/json or application/x-www-form-urlencoded")
        if not isinstance(payload, dict):
            raise ValueError("task action body must be an object")
        csrf_token = payload.pop("csrf_token", request.headers.get("x-agent-chronicle-csrf"))
        if not isinstance(csrf_token, str) or not secrets.compare_digest(csrf_token, expected_csrf_token):
            raise HTTPException(status_code=403, detail="invalid task action CSRF token")
        return model.model_validate(payload)
    except HTTPException:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid task action body") from exc


def _fmt_usd(value: Any) -> str:
    try:
        number = float(value or 0.0)
    except (OverflowError, TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number):
        number = 0.0
    return f"${number:,.2f}"


def _fmt_optional_usd(value: Any) -> str:
    if value is None:
        return '<span class="note">No estimate</span>'
    return _fmt_usd(value)


def _fmt_optional_usd_text(value: Any) -> str:
    if value is None:
        return "No estimate"
    return _fmt_usd(value)


def _fmt_timeline_cost_cell(value: Any) -> str:
    if value is None:
        return '<span class="note">No cost</span>'
    return _fmt_optional_usd(value)


def _fmt_int(value: Any) -> str:
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        number = 0
    return f"{number:,}"


def _fmt_compact_int(value: Any) -> str:
    """Compact large dashboard-card values without changing ledger totals."""

    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        number = 0
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(number) >= threshold:
            label = f"{number / threshold:.2f}".rstrip("0").rstrip(".")
            return f"{label}{suffix}"
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


def _fmt_date(value: Any) -> str:
    """YYYY-MM-DD, with the same hostile-timestamp tolerance as _fmt_time."""
    formatted = _fmt_time(value)
    return formatted.split(" ")[0] if formatted else ""


def _fmt_duration(value: Any) -> str:
    try:
        seconds = float(value or 0.0)
    except (OverflowError, TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        return ""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    return f"{minutes}m {remaining}s"


def _fmt_duration_hm(value: Any) -> str:
    """Humanized h/m session duration (PRD §6.2), '' when not displayable.

    The ledger only serves guarded non-negative ``duration_seconds`` (or
    None), but the formatter tolerates anything with the usual hostile-value
    rules. A true sub-minute span renders "<1m" — a real short session, not
    an absent value (absent durations are omitted by the caller).
    """

    try:
        seconds = float(value)
    except (OverflowError, TypeError, ValueError):
        return ""
    if not math.isfinite(seconds) or seconds < 0:
        return ""
    hours, minutes = divmod(int(seconds // 60), 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def _cap_note(shown: int, total: int, noun: str) -> str:
    """Truncation indicator for capped tables: '' unless actually truncated.

    Silent caps are how the one attributed row disappeared from view — every
    live table slice must pass through capped_rows and render this note.
    """

    shown_count = max(0, int(shown or 0))
    total_count = max(0, int(total or 0))
    if total_count <= shown_count:
        return ""
    return (
        '<p class="section-note">Showing '
        f"{html.escape(_fmt_int(shown_count))} of {html.escape(_fmt_int(total_count))} {html.escape(noun)}.</p>"
    )


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _metadata_int(event: dict[str, Any], key: str) -> int:
    try:
        number = int(_metadata(event).get(key) or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _cache_creation_tokens(event: dict[str, Any]) -> int:
    value = _metadata_int(event, "cache_creation_input_tokens")
    if value > 0:
        return value
    metadata = _metadata(event)
    if "cache_read_input_tokens" not in metadata:
        return 0
    cached = _metadata_int(event, "cached_input_tokens")
    read = _metadata_int(event, "cache_read_input_tokens")
    return max(0, cached - read)


def _cache_read_tokens(event: dict[str, Any]) -> int:
    value = _metadata_int(event, "cache_read_input_tokens")
    if value > 0:
        return value
    cached = _metadata_int(event, "cached_input_tokens")
    creation = _metadata_int(event, "cache_creation_input_tokens")
    return max(0, cached - creation)


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


def _session_activity_time(event: dict[str, Any]) -> float:
    metadata = _metadata(event)
    client = str(metadata.get("client") or "").strip()
    if client == "hermes":
        preferred = metadata.get("started_at") or metadata.get("updated_at")
    else:
        preferred = metadata.get("updated_at") or metadata.get("started_at")
    try:
        timestamp = float(preferred or event.get("created_at") or 0.0)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    return timestamp if math.isfinite(timestamp) and timestamp > 0 else 0.0


def _is_usage_activity(event: dict[str, Any]) -> bool:
    return is_local_usage_import_event(event)


def _short_session_label(client: Any, session_id: Any, session_labels: dict[tuple[str, str], str]) -> str:
    """Raw-tab session label: NEVER the full id (locked line: hrefs only).

    Prefers the ledger rollup's collision-suffixed label; sessions that never
    formed a rollup entry (e.g. context-only diagnostics) fall back to the
    shared base truncation rule.
    """

    if session_id is None or str(session_id) == "":
        return "Unknown session"
    key = (str(client or ""), str(session_id))
    return session_labels.get(key) or _base_session_display_label(client, str(session_id)) or "Unknown session"


def _record_session_label_cell(
    record: DashboardUsageRecord, esc: Any, session_labels: dict[tuple[str, str], str]
) -> str:
    label = _short_session_label(record.client, record.session_id, session_labels)
    details: list[str] = []
    if record.project_dir:
        # Shared redaction rule (work_ledger): leaf segment only, home renders
        # "~", and `.claude/worktrees/<name>` paths label as the OWNING repo
        # with an explicit "(worktree)" marker.
        project_label, project_source = _project_label_info(record.project_dir)
        if project_label:
            details.append(project_label + (" (worktree)" if project_source == "claude_worktree" else ""))
    details.append(_record_kind_label(record.session_kind))
    if record.source_file and record.source_file != "state.db":
        details.append(_short_text(record.source_file, max_length=32))
    note = f"<br><span class=\"note\">{esc(' / '.join(details))}</span>" if details else ""
    return f"<code>{esc(label)}</code>{note}"


def _reasoning_label(event: dict[str, Any]) -> str:
    client = str(_metadata(event).get("client") or event.get("source") or "")
    if client in {"claude-code", "claude-code-local-session-import"}:
        return "reasoning not reported"
    return f"{_fmt_int(_metadata_int(event, 'reasoning_output_tokens'))} reasoning"


def _record_reasoning_label(record: DashboardUsageRecord) -> str:
    if record.client == "claude-code":
        return "reasoning not reported"
    return f"{_fmt_int(record.reasoning_output_tokens)} reasoning"


@dataclass(frozen=True)
class DashboardUsageRecord:
    client: str
    provider: str
    model: str | None
    session_id: str
    session_kind: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    usage_additive: bool = True
    usage_normalization_state: str | None = None
    raw_cumulative_input_tokens: int | None = None
    raw_cumulative_output_tokens: int | None = None
    raw_cumulative_cached_input_tokens: int | None = None
    cache_creation_tokens_reported: bool | None = None
    cache_read_tokens_reported: bool | None = None
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    estimated_cost_usd: float | None = None
    client_reported_cost_usd: float | None = None
    usage_confidence: str | None = None
    cost_confidence: str | None = None
    started_at: float | None = None
    updated_at: float | None = None
    source_file: str | None = None
    project_dir: str | None = None
    raw_event: dict[str, Any] | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_tokens_including_cached(self) -> int:
        return self.input_tokens + self.output_tokens + self.cached_input_tokens


@dataclass(frozen=True)
class DashboardUsageView:
    local_records: list[DashboardUsageRecord]
    saved_records: list[DashboardUsageRecord]
    excluded_local_records: list[DashboardUsageRecord]
    excluded_saved_records: list[DashboardUsageRecord]
    local_by_client: dict[str, list[DashboardUsageRecord]]
    saved_by_client: dict[str, list[DashboardUsageRecord]]
    # Recording calls agentacct REFUSED, derived from the same live client-log
    # scan that produced local_records. Nothing about a refusal is stored, so
    # this is re-derived on every scan — which is also why refusals that
    # predate the feature are counted without a backfill.
    refused_recording: dict[str, Any] = dataclass_field(default_factory=dict)


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0


def _record_kind_label(value: Any) -> str:
    kind = str(value or "root").strip().lower()
    labels = {
        "root": "main",
        "child": "child",
        "internal": "auto-review",
    }
    return labels.get(kind, kind or "main")


def _usage_record_count_note(stats: dict[str, Any]) -> str:
    parts = []
    for key, label in (("main_sessions", "main"), ("child_sessions", "child"), ("internal_sessions", "auto-review")):
        value = int(stats.get(key) or 0)
        if value:
            parts.append(f"{_fmt_int(value)} {label}")
    return " / ".join(parts)


def _usage_record_count_cell(stats: dict[str, Any], esc: Any) -> str:
    value = int(stats.get("records") or 0)
    note = _usage_record_count_note(stats)
    if not note or note == f"{_fmt_int(value)} main":
        return esc(_fmt_int(value))
    return f"{esc(_fmt_int(value))}<br><span class=\"note\">{esc(note)}</span>"


def _usage_record_time(record: DashboardUsageRecord) -> float:
    preferred = record.started_at if record.client == "hermes" else record.updated_at
    timestamp = preferred or record.updated_at or record.started_at
    if timestamp and math.isfinite(timestamp) and timestamp > 0:
        return timestamp
    if record.raw_event is not None:
        return _session_activity_time(record.raw_event)
    return 0.0


def _record_from_client_usage(event: ClientUsageEvent) -> DashboardUsageRecord:
    estimated_cost = _estimated_equivalent_cost(event)
    usage_additive, normalization_state = local_usage_additivity(
        client=event.client,
        session_kind=event.client_session_kind,
        parent_client_session_id=event.parent_client_session_id,
        usage_update_semantics=event.usage_update_semantics,
        source_namespace_fingerprint=event.source_namespace_fingerprint,
        parent_source_namespace_fingerprint=(
            event.source_namespace_fingerprint if event.parent_client_session_id else None
        ),
    )
    return DashboardUsageRecord(
        client=event.client,
        provider=event.provider,
        model=event.model,
        session_id=event.client_session_id,
        session_kind=event.client_session_kind or "root",
        input_tokens=event.input_tokens if usage_additive else 0,
        output_tokens=event.output_tokens if usage_additive else 0,
        cached_input_tokens=event.cached_input_tokens if usage_additive else 0,
        cache_creation_input_tokens=event.cache_creation_input_tokens if usage_additive else 0,
        cache_read_input_tokens=event.cache_read_input_tokens if usage_additive else 0,
        usage_additive=usage_additive,
        usage_normalization_state=normalization_state,
        raw_cumulative_input_tokens=event.input_tokens if not usage_additive else None,
        raw_cumulative_output_tokens=event.output_tokens if not usage_additive else None,
        raw_cumulative_cached_input_tokens=event.cached_input_tokens if not usage_additive else None,
        cache_creation_tokens_reported=getattr(event, "cache_creation_tokens_reported", None),
        cache_read_tokens_reported=getattr(event, "cache_read_tokens_reported", None),
        cache_creation_5m_input_tokens=event.cache_creation_5m_input_tokens if usage_additive else 0,
        cache_creation_1h_input_tokens=event.cache_creation_1h_input_tokens if usage_additive else 0,
        reasoning_output_tokens=event.reasoning_output_tokens if usage_additive else 0,
        estimated_cost_usd=estimated_cost if usage_additive else None,
        client_reported_cost_usd=event.client_reported_cost_usd if usage_additive else None,
        usage_confidence="client_reported",
        cost_confidence=(
            "client_reported"
            if usage_additive and event.client_reported_cost_usd is not None
            else "estimated_from_tokens"
            if usage_additive and estimated_cost is not None
            else None
        ),
        started_at=float(event.started_at) if event.started_at is not None else None,
        updated_at=float(event.updated_at) if event.updated_at is not None else None,
        source_file=event.source_path.name,
        project_dir=event.cwd,
    )


def _record_from_sentinel_event(event: dict[str, Any]) -> DashboardUsageRecord | None:
    if not _is_usage_activity(event):
        return None
    metadata = _metadata(event)
    client = str(metadata.get("client") or event.get("source") or "unknown")
    # Import rows only (guaranteed by _is_usage_activity): normalize away our
    # own legacy ':model:' key artifact so un-migrated stores stop showing
    # phantom per-model sessions. Only claude-code rows ever carried the
    # marker; child ':stem' suffixes are preserved.
    session_id = normalized_local_usage_session_id(
        metadata.get("client"), str(metadata.get("client_session_id") or event.get("run_id") or "")
    )
    cached = _metadata_int(event, "cached_input_tokens")
    cache_creation = _cache_creation_tokens(event)
    cache_read = _cache_read_tokens(event)
    cost = _safe_float(event.get("estimated_cost_usd"))
    cost_confidence = str(event.get("cost_confidence") or "") or None
    usage_additive, normalization_state = local_usage_additivity(
        client=client,
        session_kind=metadata.get("client_session_kind"),
        parent_client_session_id=metadata.get("parent_client_session_id"),
        usage_update_semantics=metadata.get("usage_update_semantics"),
        explicit_usage_additive=metadata.get("usage_additive"),
        source_namespace_fingerprint=metadata.get("source_namespace_fingerprint"),
        parent_source_namespace_fingerprint=metadata.get("parent_source_namespace_fingerprint"),
    )
    input_tokens = _safe_nonnegative_event_int(event, "estimated_input_tokens")
    output_tokens = _safe_nonnegative_event_int(event, "estimated_output_tokens")
    return DashboardUsageRecord(
        client=client,
        provider=str(event.get("provider") or client),
        model=str(event.get("model")) if event.get("model") else None,
        session_id=session_id,
        session_kind=str(metadata.get("client_session_kind") or "root"),
        input_tokens=input_tokens if usage_additive else 0,
        output_tokens=output_tokens if usage_additive else 0,
        cached_input_tokens=cached if usage_additive else 0,
        cache_creation_input_tokens=cache_creation if usage_additive else 0,
        cache_read_input_tokens=cache_read if usage_additive else 0,
        usage_additive=usage_additive,
        usage_normalization_state=normalization_state,
        raw_cumulative_input_tokens=input_tokens if not usage_additive else None,
        raw_cumulative_output_tokens=output_tokens if not usage_additive else None,
        raw_cumulative_cached_input_tokens=cached if not usage_additive else None,
        cache_creation_tokens_reported=(
            bool(metadata.get("cache_creation_tokens_reported"))
            if isinstance(metadata.get("cache_creation_tokens_reported"), bool)
            else None
        ),
        cache_read_tokens_reported=(
            bool(metadata.get("cache_read_tokens_reported"))
            if isinstance(metadata.get("cache_read_tokens_reported"), bool)
            else None
        ),
        cache_creation_5m_input_tokens=_metadata_int(event, "cache_creation_5m_input_tokens"),
        cache_creation_1h_input_tokens=_metadata_int(event, "cache_creation_1h_input_tokens"),
        reasoning_output_tokens=_metadata_int(event, "reasoning_output_tokens") if usage_additive else 0,
        estimated_cost_usd=cost if usage_additive else None,
        client_reported_cost_usd=cost if usage_additive and cost_confidence == "client_reported" else None,
        usage_confidence=str(event.get("usage_confidence") or "") or None,
        cost_confidence=cost_confidence if usage_additive else None,
        started_at=_safe_float(metadata.get("started_at")),
        updated_at=_safe_float(metadata.get("updated_at")),
        source_file=str(metadata.get("source_file")) if metadata.get("source_file") else None,
        project_dir=str(metadata.get("project_dir")) if metadata.get("project_dir") else None,
        raw_event=event,
    )


def _safe_nonnegative_event_int(event: dict[str, Any], key: str) -> int:
    try:
        value = int(event.get(key) or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _usage_records_by_client(records: list[DashboardUsageRecord]) -> dict[str, list[DashboardUsageRecord]]:
    by_client: dict[str, list[DashboardUsageRecord]] = {}
    for record in records:
        by_client.setdefault(record.client, []).append(record)
    return by_client


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


def _build_usage_view(local_usage_preview: list[ClientUsageEvent], events: list[dict[str, Any]]) -> DashboardUsageView:
    all_local_records = [_record_from_client_usage(event) for event in local_usage_preview]
    # Shared legacy-store rule (usage_truth): stale claude-code base rows
    # shadowed by ':model:' siblings are excluded exactly like the work ledger
    # and the context bridge exclude them, so all three surfaces agree.
    kept_events, _shadowed = split_shadowed_legacy_usage_events(events)
    ambiguous_source_identities = (
        local_usage_source_namespace_ambiguous_identities(kept_events)
    )
    all_saved_records = [record for event in kept_events if (record := _record_from_sentinel_event(event)) is not None]
    if ambiguous_source_identities:
        all_saved_records = [
            _quarantine_ambiguous_dashboard_usage_record(
                record,
                ambiguous_source_identities,
            )
            for record in all_saved_records
        ]
    local_records = [record for record in all_local_records if record.usage_additive]
    excluded_local_records = [record for record in all_local_records if not record.usage_additive]
    saved_records = [record for record in all_saved_records if record.usage_additive]
    excluded_saved_records = [record for record in all_saved_records if not record.usage_additive]
    return DashboardUsageView(
        local_records=local_records,
        saved_records=saved_records,
        excluded_local_records=excluded_local_records,
        excluded_saved_records=excluded_saved_records,
        local_by_client=_usage_records_by_client(local_records),
        saved_by_client=_usage_records_by_client(saved_records),
        # Derived from the raw scan candidates (not the records), because the
        # summary dedups per base session itself: one Claude Code transcript
        # becomes several per-model records carrying the SAME refusal counts.
        refused_recording=summarize_refused_recording_attempts(local_usage_preview),
    )


def _quarantine_ambiguous_dashboard_usage_record(
    record: DashboardUsageRecord,
    ambiguous_identities: set[tuple[str, str, str]],
) -> DashboardUsageRecord:
    raw_event = record.raw_event
    identity = (
        recognized_local_usage_row_identity(raw_event)
        if isinstance(raw_event, dict)
        else None
    )
    if identity not in ambiguous_identities:
        return record
    return replace(
        record,
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        usage_additive=False,
        usage_normalization_state="source_namespace_missing_vs_explicit",
        raw_cumulative_input_tokens=record.input_tokens,
        raw_cumulative_output_tokens=record.output_tokens,
        raw_cumulative_cached_input_tokens=record.cached_input_tokens,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        reasoning_output_tokens=0,
        estimated_cost_usd=None,
        client_reported_cost_usd=None,
        cost_confidence=None,
    )


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


def _dashboard_importer_version() -> str:
    return importer_build_id()


# A dashboard refresh used to redirect to ``/?imported=…&scanned=…&…`` — an 18-
# field query string that cluttered the address bar on every refresh. The
# summary now rides a one-time cookie instead, so the URL stays a clean ``/`` and
# the post-refresh banner still shows once (then the cookie is cleared on read).
REFRESH_FLASH_COOKIE = "chronicle_refresh"
REFRESH_FLASH_MAX_AGE = 30  # seconds; a self-expiring floor if a read never happens


def _encode_refresh_flash(status: Mapping[str, Any]) -> str:
    """Compact, cookie-safe encoding of a refresh summary (base64 of JSON)."""

    raw = json.dumps(dict(status), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_refresh_flash(value: str | None) -> dict[str, Any] | None:
    """Inverse of :func:`_encode_refresh_flash`; None on anything malformed."""

    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# --- Live refresh progress (background thread + no-JS /refreshing page) -------
_REFRESH_CLIENT_LABELS = {
    "codex": "Codex",
    "claude-code": "Claude Code",
    "cursor": "Cursor",
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "opencode": "OpenCode",
}
# Rough weights so the overall bar advances sensibly across a scan; claude-code
# is the slow one, and the evidence reconcile is a big count-less tail. The
# scanning band tops out near 0.60, reconcile carries it to ~0.92, done = 1.0.
_REFRESH_SCAN_WEIGHTS = {
    "codex": 0.12,
    "claude-code": 0.40,
    "cursor": 0.02,
    "hermes": 0.02,
    "openclaw": 0.02,
    "opencode": 0.02,
}
# A refresh still "running" after this long is treated as dead so a new one can
# start (the daemon thread may have died without reporting).
_REFRESH_STALE_AFTER_S = 300.0


class RefreshProgress:
    """Thread-safe, single-flight progress for a dashboard refresh that runs on
    a background thread. The worker calls phase()/scan()/finish()/fail() (scan
    and phase are the reporter interface :func:`usage_progress_scope` expects);
    the /refreshing status page reads snapshot() from the main thread. Only one
    refresh runs at a time — a second click joins the running one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job_id = 0
        self._state = "idle"  # idle | running | done | error
        self._started = 0.0
        self._phase = ""
        self._clients: dict[str, dict[str, int]] = {}
        self._fraction = 0.0
        self._result: dict[str, Any] | None = None
        self._error: str | None = None
        self._consumed = False

    def begin(self) -> int | None:
        """Claim the single job slot; returns a job id, or None if a fresh
        refresh is already running (the caller should just watch that one)."""

        with self._lock:
            now = time.monotonic()
            if self._state == "running" and (now - self._started) < _REFRESH_STALE_AFTER_S:
                return None
            self._job_id += 1
            self._state = "running"
            self._started = now
            self._phase = "Starting"
            self._clients = {}
            self._fraction = 0.0
            self._result = None
            self._error = None
            self._consumed = False
            return self._job_id

    def scan(self, client: str, scanned: int, total: int) -> None:
        with self._lock:
            if self._state != "running":
                return
            self._clients[str(client)] = {"scanned": int(scanned), "total": int(total)}
            self._phase = f"Scanning {_REFRESH_CLIENT_LABELS.get(client, client)} logs"
            frac = 0.0
            for name, prog in self._clients.items():
                weight = _REFRESH_SCAN_WEIGHTS.get(name, 0.02)
                total_files = prog["total"] or 0
                done = (prog["scanned"] / total_files) if total_files else 1.0
                frac += weight * min(1.0, done)
            self._fraction = max(self._fraction, min(0.60, frac))

    def phase(self, label: str) -> None:
        with self._lock:
            if self._state != "running":
                return
            self._phase = str(label)
            low = str(label).lower()
            if "reconcil" in low or "evidence" in low:
                self._fraction = max(self._fraction, 0.62)
            elif "pric" in low or "finish" in low or "writ" in low:
                self._fraction = max(self._fraction, 0.92)

    def finish(self, result: Mapping[str, Any]) -> None:
        with self._lock:
            self._state = "done"
            self._result = dict(result)
            self._phase = "Done"
            self._fraction = 1.0

    def fail(self, error: str) -> None:
        with self._lock:
            self._state = "error"
            self._error = str(error)[:300]
            self._phase = "Failed"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self._job_id,
                "state": self._state,
                "phase": self._phase,
                "clients": {k: dict(v) for k, v in self._clients.items()},
                "fraction": round(self._fraction, 4),
                "error": self._error,
                "consumed": self._consumed,
                "elapsed": (time.monotonic() - self._started) if self._started else 0.0,
            }

    def consume_result(self) -> dict[str, Any] | None:
        """Return the finished result exactly once, then mark it consumed so a
        reload of / does not re-flash the banner. None if not done/already
        consumed."""

        with self._lock:
            if self._state == "done" and not self._consumed:
                self._consumed = True
                return dict(self._result) if self._result else {}
            return None


_REFRESHING_CSS = """
:root{--bg:#f4f6f9;--card:#fff;--ink:#1b2330;--muted:#66748a;--line:#e2e8f1;--track:#eef1f6;--iris:#5560e0;--good:#2f9e51;--shadow:0 8px 30px rgba(20,30,50,.08);--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--ink:#e7edf5;--muted:#93a1b4;--line:#232b36;--track:#1b222c;--iris:#7c8bff;--good:#46b869;--shadow:0 10px 34px rgba(0,0,0,.4)}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:30px 30px 24px;width:100%;max-width:430px;text-align:center}
h1{font-size:19px;margin:14px 0 18px;letter-spacing:-.01em}
.spinner{width:34px;height:34px;margin:0 auto;border-radius:50%;border:3px solid var(--track);border-top-color:var(--iris);animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.bar-track{height:8px;border-radius:999px;background:var(--track);overflow:hidden}
.bar-fill{height:100%;background:var(--iris);border-radius:999px;transition:width .5s ease}
.bar-fill.shimmer{background:repeating-linear-gradient(45deg,var(--iris),var(--iris) 11px,rgba(255,255,255,.28) 11px,rgba(255,255,255,.28) 22px);background-size:31px 31px;animation:sh .7s linear infinite}
@keyframes sh{to{background-position:31px 0}}
.meta{display:flex;justify-content:space-between;align-items:baseline;margin-top:10px}
.phase{font-size:14px;font-weight:500}
.pct{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px;color:var(--muted)}
.clients{list-style:none;margin:18px 0 0;padding:0;text-align:left;display:flex;flex-direction:column;gap:7px}
.clients li{display:flex;align-items:center;gap:8px;font-size:13.5px}
.cl{color:var(--muted);flex:1}
.ct{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink)}
.ct .sep{color:var(--muted);margin:0 3px}
.ok{color:var(--good);font-weight:700}.dots{color:var(--muted)}
.hint{color:var(--muted);font-size:12.5px;margin:20px 0 0}
.err .ico{font-size:30px}.err .msg{color:var(--muted);font-size:13.5px;word-break:break-word;margin:8px 0 0}
.back{display:inline-block;margin-top:14px;color:var(--iris);text-decoration:none;font-weight:600;font-size:14px}
.back:hover{text-decoration:underline}
@media (prefers-reduced-motion:reduce){.spinner,.bar-fill.shimmer{animation:none}}
"""


def _render_refreshing_page(snap: Mapping[str, Any]) -> str:
    """The no-JS refresh progress page. While running it carries a 1-second
    meta-refresh (the only no-JS way to poll); the server 303s to / when done,
    so the loop ends on its own. On error it shows the reason with a way back."""

    esc = html.escape
    state = snap.get("state")
    phase = str(snap.get("phase") or "")
    fraction = float(snap.get("fraction") or 0.0)
    pct = max(0, min(100, int(round(fraction * 100))))
    running = state == "running"
    reconciling = ("reconcil" in phase.lower()) or ("evidence" in phase.lower())

    rows: list[str] = []
    clients = snap.get("clients") or {}
    for name in ("codex", "claude-code", "cursor", "hermes", "openclaw", "opencode"):
        prog = clients.get(name)
        if not prog:
            continue
        label = _REFRESH_CLIENT_LABELS.get(name, name)
        scanned = int(prog.get("scanned") or 0)
        total = int(prog.get("total") or 0)
        mark = '<span class="ok">&#10003;</span>' if (total and scanned >= total) else '<span class="dots">&hellip;</span>'
        rows.append(
            f'<li><span class="cl">{esc(label)}</span>'
            f'<span class="ct">{scanned:,}<span class="sep">/</span>{total:,}</span>{mark}</li>'
        )
    client_html = "".join(rows) or '<li><span class="cl">Looking for agent logs&hellip;</span></li>'

    meta_refresh = '<meta http-equiv="refresh" content="1">' if running else ""

    if state == "error":
        body = (
            '<div class="card err">'
            '<div class="ico">&#9888;</div>'
            "<h1>Refresh didn&rsquo;t finish</h1>"
            f'<p class="msg">{esc(str(snap.get("error") or "Unknown error"))}</p>'
            '<a class="back" href="/">Back to dashboard</a>'
            "</div>"
        )
    else:
        bar_class = "bar-fill shimmer" if reconciling else "bar-fill"
        body = (
            '<div class="card">'
            '<div class="spinner"></div>'
            "<h1>Refreshing your agent logs</h1>"
            f'<div class="bar-track"><div class="{bar_class}" style="width:{pct}%"></div></div>'
            f'<div class="meta"><span class="phase">{esc(phase or "Starting")}&hellip;</span>'
            f'<span class="pct">{pct}%</span></div>'
            f'<ul class="clients">{client_html}</ul>'
            '<p class="hint">This runs in the background &mdash; you&rsquo;ll land back on the '
            "dashboard when it&rsquo;s done.</p>"
            "</div>"
        )

    return (
        "<!doctype html>\n<html lang=\"en\"><head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{meta_refresh}\n"
        "<title>Refreshing&hellip; &middot; agentacct</title>\n"
        f"<style>{_REFRESHING_CSS}</style>\n"
        f"</head><body>{body}</body></html>"
    )


def _evidence_reconcile_redirect_status(outcome: Mapping[str, Any]) -> dict[str, int | bool]:
    """Bounded, path-free Evidence status safe for a Dashboard redirect."""

    errors = outcome.get("errors")
    error_count = (
        len(errors)
        if isinstance(errors, (list, tuple))
        else int(bool(errors))
    )
    conflict_count = 0
    for key in ("conflicts", "existing_conflicts"):
        try:
            conflict_count += max(int(outcome.get(key) or 0), 0)
        except (TypeError, ValueError, OverflowError):
            continue
    return {
        "evidence_reconcile_enabled": outcome.get("enabled") is not False,
        "evidence_reconcile_errors": error_count,
        "evidence_reconcile_conflicts": conflict_count,
        "evidence_reconcile_complete": outcome.get("complete_applied") is True,
    }


def _import_local_usage_events(
    service: SentinelService,
    usage_config: UsageDiscoveryConfig,
    ingestion_health: IngestionHealthStore,
    *,
    limit_sessions: int = DASHBOARD_USAGE_LIMIT_SESSIONS,
    client: str = "all",
) -> dict[str, Any]:
    if not usage_config.enabled:
        return {
            "scanned": 0,
            "imported": 0,
            "refreshed": 0,
            "migrated": 0,
            "priced": 0,
            "repriced": 0,
            "source_namespace_conflicts": 0,
            "source_namespace_adoptions": 0,
            "concurrent_refresh_conflicts": 0,
            "incomplete_alias_migrations": 0,
            "observed_sessions": 0,
            "sessions_without_usage": 0,
            "imported_session_observations": 0,
            "preserved_session_observations": 0,
            "session_observation_conflicts": 0,
            "usage_sessions": 0,
        }

    selected_sources = SUPPORTED_CLIENTS if client == "all" else (client,)
    if any(source not in SUPPORTED_CLIENTS for source in selected_sources):
        raise ValueError("unsupported local usage client")
    scan_id = ingestion_health.begin_scan(
        sources=selected_sources,
        scan_limit=limit_sessions,
        importer_version=_dashboard_importer_version(),
    )
    try:
        expected_observation_conflicts = (
            service.trusted_session_observation_conflict_snapshot()
        )
        with codex_parse_cache_scope(service.store.root):
            discovery = _discover_local_usage(
                usage_config,
                limit_sessions=limit_sessions,
                include_diagnostics=True,
                client=client,
            )
        assert isinstance(discovery, ClientUsageDiscoveryResult)
        scanned_candidates = discovery.events
        session_observation_candidates = usage_less_session_observations(discovery)
        _emit_scan_phase("Reconciling sessions")
        complete_observation_clients = (
            complete_session_observation_reconciliation_clients(discovery)
        )
        resolved_session_observation_conflicts = (
            service.reconcile_trusted_session_observation_conflicts(
                [
                    observation.to_sentinel_event()
                    for observation in discovery.session_observations
                    if observation.client in complete_observation_clients
                ],
                expected_conflict_revisions=expected_observation_conflicts,
            )
            if complete_observation_clients
            else []
        )
        observed_session_keys = {
            observation.source_session_identity
            for observation in discovery.session_observations
        }
        observed_session_keys.update(
            candidate.source_session_identity
            for candidate in scanned_candidates
        )
        usage_session_keys = {
            candidate.source_session_identity
            for candidate in scanned_candidates
        }
        existing_event_ids = {
            str(event.get("event_id"))
            for event in service.list_all_events()
            if event.get("event_id")
        }
        recorded_session_observations: list[dict[str, Any]] = []
        session_observation_conflicts_by_client: dict[str, int] = {}
        session_observation_conflict_reasons_by_client: dict[
            str, dict[str, int]
        ] = {}
        for observation in session_observation_candidates:
            try:
                recorded_observation = service.record_trusted_session_observation(
                    observation.to_sentinel_event()
                )
            except SessionObservationConflict as exc:
                reason = session_observation_conflict_error_code(exc.reason)
                session_observation_conflicts_by_client[observation.client] = (
                    session_observation_conflicts_by_client.get(
                        observation.client,
                        0,
                    )
                    + 1
                )
                reason_counts = session_observation_conflict_reasons_by_client.setdefault(
                    observation.client,
                    {},
                )
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                continue
            if str(recorded_observation.get("event_id") or "") not in existing_event_ids:
                recorded_session_observations.append(recorded_observation)
                if recorded_observation.get("event_id"):
                    existing_event_ids.add(str(recorded_observation["event_id"]))
        current_events = service.list_all_events()
        projectable_observation_identities = (
            selected_local_session_observation_source_identities(current_events)
        )
        candidate_raw_keys = {
            (observation.client, observation.client_session_id)
            for observation in session_observation_candidates
        }
        observation_conflict_identities = sum(
            1
            for key in service.trusted_session_observation_conflict_snapshot()
            if key in candidate_raw_keys
        )
        preserved_session_observations = len(
            {
                observation.source_session_identity
                for observation in session_observation_candidates
            }
            & projectable_observation_identities
        )
        (
            candidates,
            bound_namespace_adoptions,
            binding_namespace_conflicts,
            bound_namespace_adoption_counts,
        ) = bind_discovered_usage_source_namespaces(service, scanned_candidates)
        plan = plan_local_usage_import(candidates, service.list_all_events())
        # Dashboard refresh semantics: every re-observed row whose totals
        # CHANGED is replaced with fresh totals. Legacy per-model keys are
        # superseded in the same transaction and unknown-cost rows may be
        # promoted when pricing becomes available.
        plan, repriced_candidates = promote_unknown_cost_reprices(plan)
        import_candidates = [*plan.new_candidates, *plan.refresh_candidates, *plan.migration_candidates]
        bound_namespace_adoption_identities = {
            candidate.usage_row_identity for candidate in bound_namespace_adoptions
        }
        planned_write_namespace_adoptions = [
            candidate
            for candidate in source_namespace_adoption_candidates(import_candidates, plan)
            if candidate.usage_row_identity
            not in bound_namespace_adoption_identities
        ]
        write_batches = build_usage_import_write_batches(import_candidates, plan)
        events = [
            event
            for batch_events, _predicate, _guard, _dedup_key in write_batches
            for event in batch_events
        ]
        planned_priced_identities: set[tuple[str, str, str]] = set()
        for event in events:
            if apply_pricing_estimate_to_event(event):
                identity = recognized_local_usage_row_identity(event)
                if identity is not None:
                    planned_priced_identities.add(identity)
        # Never rewrite the ledger for a no-op scan (all rows unchanged).
        if events:
            recorded = []
            for batch_events, replace_predicate, replace_guard, dedup_key in write_batches:
                recorded.extend(
                    service.replace_events(
                        replace_predicate,
                        batch_events,
                        trusted_usage_import=True,
                        dedup_key=dedup_key,
                        replace_guard=replace_guard,
                    )
                )
        else:
            recorded = []
        write_namespace_conflicts, concurrent_refresh_conflicts = (
            classify_usage_write_conflict_candidates(
                import_candidates,
                plan,
                recorded,
                service.list_all_events(),
            )
        )
        recorded_identities = {
            identity
            for event in recorded
            if (identity := recognized_local_usage_row_identity(event)) is not None
        }
        actual_migrated = sum(
            1
            for candidate in plan.migration_candidates
            if candidate.usage_row_identity in recorded_identities
        )
        actual_repriced = sum(
            1
            for candidate in repriced_candidates
            if candidate.usage_row_identity in recorded_identities
        )
        actual_priced = sum(
            1
            for event in recorded
            if recognized_local_usage_row_identity(event) in planned_priced_identities
        )
        write_namespace_adoptions = [
            candidate
            for candidate in planned_write_namespace_adoptions
            if candidate.usage_row_identity in recorded_identities
        ]
        namespace_adoption_counts = dict(bound_namespace_adoption_counts)
        for candidate in write_namespace_adoptions:
            namespace_adoption_counts[candidate.client] = (
                namespace_adoption_counts.get(candidate.client, 0) + 1
            )
        import_diagnostics = build_usage_import_diagnostics(
            discovery.diagnostics,
            namespace_conflict_candidates=[
                *binding_namespace_conflicts,
                *plan.namespace_conflict_candidates,
                *write_namespace_conflicts,
            ],
            namespace_adoption_candidates=[],
            namespace_adoption_counts=namespace_adoption_counts,
            concurrent_refresh_conflict_candidates=concurrent_refresh_conflicts,
            incomplete_migration_bases=plan.incomplete_migration_bases,
        )
        for source, conflict_count in session_observation_conflicts_by_client.items():
            diagnostic = import_diagnostics.setdefault(source, {})
            diagnostic["session_observation_conflicts"] = conflict_count
            reason_counts = dict(
                session_observation_conflict_reasons_by_client.get(source, {})
            )
            diagnostic["session_observation_conflict_reasons"] = reason_counts
            diagnostic["error_count"] = int(diagnostic.get("error_count") or 0) + conflict_count
            error_codes = [
                code
                for code in diagnostic.get("error_codes") or []
                if isinstance(code, str)
            ]
            for code in [*reason_counts, "session_observation_conflict"]:
                if code not in error_codes:
                    error_codes.append(code)
            diagnostic["error_codes"] = error_codes
        _emit_scan_phase("Reconciling evidence")
        try:
            evidence_usage_reconcile = (
                service.reconcile_evidence_refreshable_usage_snapshot(
                    complete=True,
                    transport="internal",
                )
            )
        except Exception:  # noqa: BLE001 - Evidence must not roll back a proven v1 write.
            evidence_usage_reconcile = {
                "enabled": bool(getattr(getattr(service, "evidence", None), "enabled", True)),
                "complete_requested": True,
                "complete_applied": False,
                "errors": [EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE],
            }
        if not isinstance(evidence_usage_reconcile, Mapping):
            evidence_usage_reconcile = {
                "enabled": bool(getattr(getattr(service, "evidence", None), "enabled", True)),
                "complete_requested": True,
                "complete_applied": False,
                "errors": [EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE],
            }
        evidence_usage_reconcile = dict(evidence_usage_reconcile)
        health_results = apply_evidence_refreshable_usage_health(
            health_scan_results(import_diagnostics),
            sources=selected_sources,
            outcome=evidence_usage_reconcile,
        )
        ingestion_health.complete_scan(scan_id, results=health_results)
        return {
            "scanned": len(observed_session_keys),
            "imported": len(recorded),
            "refreshed": len(recorded),
            "migrated": actual_migrated,
            "priced": actual_priced,
            "repriced": actual_repriced,
            "source_namespace_conflicts": sum(
                int(row.get("source_namespace_conflicts") or 0)
                for row in import_diagnostics.values()
            ),
            "source_namespace_adoptions": sum(namespace_adoption_counts.values()),
            "concurrent_refresh_conflicts": len(concurrent_refresh_conflicts),
            "incomplete_alias_migrations": len(plan.incomplete_migration_bases),
            "observed_sessions": len(observed_session_keys),
            "usage_sessions": len(usage_session_keys),
            "sessions_without_usage": len(session_observation_candidates),
            "imported_session_observations": len(recorded_session_observations),
            "resolved_session_observation_conflicts": len(
                resolved_session_observation_conflicts
            ),
            "preserved_session_observations": (
                preserved_session_observations
            ),
            "session_observation_conflicts": observation_conflict_identities,
            "session_observation_conflict_rows": sum(
                session_observation_conflicts_by_client.values()
            ),
            **_evidence_reconcile_redirect_status(evidence_usage_reconcile),
        }
    except Exception:
        ingestion_health.fail_scan(scan_id, error_code="import_failed")
        raise


def _human_source_evidence(evidence: str) -> str:
    labels = {
        "projects-jsonl": "Claude Code project logs",
        "state-db+rollout-jsonl": "Codex local state plus rollout logs",
        "json-event-streams-or-db": "OpenCode JSON streams or local database",
        "state-db": "Hermes local state database",
        "jsonl-logs": "OpenClaw JSONL logs",
        "primary-state-vscdb-composer-observations": "Cursor primary state database (session observations only)",
    }
    return labels.get(evidence, evidence.replace("-", " "))


def _display_count_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == "unknown":
        return "Not reported"
    return text


# Plain-language names for the frozen refusal reason vocabulary. Anything the
# vocabulary gains without a label here still renders (as its code), so a new
# reason can never be silently dropped from the table.
_REFUSED_RECORDING_REASON_LABELS = {
    "narrative_over_limit": "Text longer than the field allows",
    "files_not_project_relative": "File path was not project-relative",
    "unknown_argument": "Argument agentacct does not accept",
    "missing_argument": "Required argument was missing",
    "invalid_argument": "Argument had the wrong type or value",
    "value_over_limit": "Value above the allowed maximum",
    "value_under_limit": "Value below the allowed minimum",
    "metadata_over_size": "Metadata above the size limit",
    "incomplete_argument_group": "Arguments that must be sent together were not",
    "no_runs": "No run existed to attach the record to",
    "unknown_run_id": "Named run does not exist in this store",
    "other": "Refused for a reason this build does not name yet",
}


def _refused_recording_reason_label(reason_code: Any) -> str:
    code = str(reason_code or "other")
    return _REFUSED_RECORDING_REASON_LABELS.get(code, code)


def _refused_recording_field_label(field_name: Any) -> str:
    """No field means the refusal was about the call, not one argument.

    Deliberately NOT the dashboard's generic "Not reported": nothing is
    missing here, and an allowlisted-only field is also how an agent-invented
    argument name is kept out of this page.
    """

    return str(field_name) if field_name else "Not argument-specific"


def _refused_recording_html(summary: Mapping[str, Any], esc: Any) -> str:
    """Refusals agentacct itself returned, derived live from the client logs.

    Renders ONLY the bounded {tool, field, reason} triple and its count. No
    message text, no rejected value, no length, no path — the rejection path
    runs before the redactor, so anything else here would leak user content.
    """

    total = int(summary.get("refused_attempt_total") or 0)
    sessions = int(summary.get("sessions_with_refusals") or 0)
    unclassified = int(summary.get("unclassified_outputs_skipped") or 0)
    rows = [row for row in (summary.get("rows") or []) if isinstance(row, Mapping)]
    if not total:
        body = (
            '<p class="section-note">No refused recording calls were found in the client logs '
            "scanned above. Recording calls that agentacct rejects are stored nowhere, so this "
            "count comes from re-reading the client's own logs on every scan.</p>"
        )
    else:
        table_rows = "".join(
            "<tr>"
            f"<td>{esc(_display_count_label(row.get('tool')))}</td>"
            f"<td>{esc(_refused_recording_field_label(row.get('field')))}</td>"
            f"<td>{esc(_refused_recording_reason_label(row.get('reason_code')))}</td>"
            f"<td>{esc(_fmt_int(row.get('count')))}</td>"
            "</tr>"
            for row in rows
        )
        body = (
            f'<p class="section-note"><strong>{esc(_fmt_int(total))} recording call(s) across '
            f"{esc(_fmt_int(sessions))} client session(s) were refused by agentacct.</strong> "
            "These are attempts an agent made and this product rejected — not work you failed to "
            "record. Until they are fixed, that work has no context in the ledger, so usage rows "
            "from those sessions can look unexplained. Only the argument name and the refusal "
            "reason are shown: the rejected values never leave the client log. This counts the "
            "sessions in the local scan on this page, so it is a floor rather than an all-time "
            "total.</p>"
            f'<div class="table-wrap"><table><thead><tr><th>Tool</th><th>Argument</th>'
            f"<th>Why agentacct refused it</th><th>Attempts</th></tr></thead>"
            f"<tbody>{table_rows}</tbody></table></div>"
        )
    remainder_note = (
        f'<p class="section-note">A further {esc(_fmt_int(unclassified))} scanned output(s) '
        "donated no recorded event but are NOT counted above: they were refused by the client "
        "before agentacct saw them, or carried a shape this build does not recognise. They stay "
        "out of the refusal total rather than inflating it.</p>"
        if unclassified
        else ""
    )
    return f"""
      <div class="subsection-title">Recording calls agentacct refused</div>
      <div class="section-header" id="refused-recording-attempts"><h2>Recording calls agentacct refused</h2></div>
      {body}
      {remainder_note}
    """


def _display_provider_model(provider: Any, model: Any) -> str:
    provider_label = _display_count_label(provider)
    model_label = _display_count_label(model)
    if provider_label == "Not reported" and model_label == "Not reported":
        return "Not reported"
    if model_label == "Not reported":
        return provider_label
    if provider_label == "Not reported":
        return model_label
    return f"{provider_label} / {model_label}"


def _human_event_type(value: Any) -> str:
    labels = {
        "model_usage": "Usage",
        "section_started": "Section started",
        "section_checkpoint": "Section checkpoint",
        "section_completed": "Section completed",
        "section_blocked": "Section blocked",
        "client_context_attached": "Client context attached",
        "instrumentation_installed": "Instrumentation installed",
        "agent_usage_debug_reported": "Agent usage report",
        "machine_check": "Machine check",
        "mcp_doctor_test": "Doctor test (diagnostic)",
        "workflow_smoke": "Workflow smoke (diagnostic)",
        "mcp_workflow_smoke": "Workflow smoke (diagnostic)",
        "budget_decision": "Budget decision",
        "task_started": "Task started (legacy)",
        "task_completed": "Task completed (legacy)",
        "task_blocked": "Task blocked (legacy)",
        "checkpoint": "Checkpoint",
        "note": "Note",
    }
    text = str(value or "").strip()
    return labels.get(text, text.replace("_", " ").title() if text else "Activity")


def _raw_event_activity_cell(event: dict[str, Any], esc: Any) -> str:
    """Activity cell for the raw agentacct event log.

    agentacct's own diagnostic tool traffic (doctor probe / workflow smoke)
    stays visible in the raw log but always carries a self-test badge. The
    check is usage_truth.is_diagnostic_event (event_type OR source) — the ONE
    diagnostic rule; never re-derived here, so a smoke event shaped like a
    normal event type is still labeled.
    """

    label = esc(_human_event_type(event.get("event_type")))
    if is_diagnostic_event(event):
        return f'{label}<br><span class="status status-needs-import">self-test</span>'
    return label


def _estimated_equivalent_cost(event: ClientUsageEvent) -> float | None:
    if not event.model or not has_model_price(event.provider, event.model):
        return None
    return estimate_model_cost_breakdown_usd(
        event.provider,
        event.model,
        input_tokens=event.input_tokens,
        output_tokens=event.output_tokens,
        cache_creation_input_tokens=event.cache_creation_input_tokens,
        cache_read_input_tokens=event.cache_read_input_tokens,
        cache_creation_5m_input_tokens=event.cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=event.cache_creation_1h_input_tokens,
    )["total_cost_usd"]


def _estimated_cost_parts(
    provider: str,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_5m_input_tokens: int = 0,
    cache_creation_1h_input_tokens: int = 0,
) -> dict[str, float] | None:
    if not model or not has_model_price(provider, model):
        return None
    return estimate_model_cost_breakdown_usd(
        provider,
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_5m_input_tokens=cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=cache_creation_1h_input_tokens,
    )


def _cost_breakdown_rows(records: list[DashboardUsageRecord]) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        provider = record.provider
        model = record.model
        parts = _estimated_cost_parts(
            provider,
            model,
            record.input_tokens,
            record.output_tokens,
            record.cache_creation_input_tokens,
            record.cache_read_input_tokens,
            record.cache_creation_5m_input_tokens,
            record.cache_creation_1h_input_tokens,
        )
        key = (record.client, provider, model or "")
        row = rows.setdefault(
            key,
            {
                "client": record.client,
                "provider": provider,
                "model": model,
                "records": 0,
                "sessions": 0,
                "main_sessions": 0,
                "child_sessions": 0,
                "internal_sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "cache_creation_cost_usd": 0.0,
                "cache_read_cost_usd": 0.0,
                "total_cost_usd": 0.0,
                "priced_records": 0,
                "unpriced_records": 0,
            },
        )
        row["records"] += 1
        row["sessions"] += 1
        kind = record.session_kind.lower()
        if kind == "internal":
            row["internal_sessions"] += 1
        elif kind == "child":
            row["child_sessions"] += 1
        else:
            row["main_sessions"] += 1
        row["input_tokens"] += record.input_tokens
        row["output_tokens"] += record.output_tokens
        row["cached_input_tokens"] += record.cached_input_tokens
        row["cache_creation_input_tokens"] += record.cache_creation_input_tokens
        row["cache_read_input_tokens"] += record.cache_read_input_tokens
        if parts is None:
            row["unpriced_records"] += 1
            continue
        row["priced_records"] += 1
        row["input_cost_usd"] += parts["input_cost_usd"]
        row["output_cost_usd"] += parts["output_cost_usd"]
        row["cache_creation_cost_usd"] += parts["cache_creation_cost_usd"]
        row["cache_read_cost_usd"] += parts["cache_read_cost_usd"]
        row["total_cost_usd"] += parts["total_cost_usd"]
    return list(rows.values())


def _provider_usage_rows(records: list[DashboardUsageRecord]) -> list[tuple[str, dict[str, Any]]]:
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        provider = record.provider or "unknown"
        row = rows.setdefault(
            provider,
            {
                "event_count": 0,
                "estimated_cost_usd": 0.0,
                "estimated_input_tokens": 0,
                "estimated_output_tokens": 0,
                "estimated_total_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens_including_cached": 0,
                "records": 0,
                "main_sessions": 0,
                "child_sessions": 0,
                "internal_sessions": 0,
            },
        )
        row["event_count"] += 1
        row["records"] += 1
        kind = record.session_kind.lower()
        if kind == "internal":
            row["internal_sessions"] += 1
        elif kind == "child":
            row["child_sessions"] += 1
        else:
            row["main_sessions"] += 1
        row["estimated_cost_usd"] += float(record.estimated_cost_usd or 0.0)
        row["estimated_input_tokens"] += record.input_tokens
        row["estimated_output_tokens"] += record.output_tokens
        row["estimated_total_tokens"] += record.total_tokens
        row["cached_input_tokens"] += record.cached_input_tokens
        row["cache_creation_input_tokens"] += record.cache_creation_input_tokens
        row["cache_read_input_tokens"] += record.cache_read_input_tokens
        row["reasoning_output_tokens"] += record.reasoning_output_tokens
        row["total_tokens_including_cached"] += record.total_tokens_including_cached
    return sorted(rows.items())

# Confidence normalization moved to usage_cube (Phase 3.5b): ONE rule shared
# by the confidence tables here and the cube's dominant-confidence buckets.
_normalized_cost_confidence = normalized_cost_confidence
_normalized_usage_confidence = normalized_usage_confidence


def _fmt_percent(numerator: int, denominator: int) -> str:
    # No denominator means "nothing to grade", not a measured 0% (PRD §10.6).
    if denominator <= 0:
        return "—"
    return f"{(numerator / denominator) * 100:.0f}%"


def _fmt_coverage_ratio(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _cost_confidence_breakdown(records: list[DashboardUsageRecord], cost_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = ["provider_billed", "client_reported", "estimated_from_tokens", "unknown"]
    rows = {
        key: {"confidence": key, "records": 0, "tokens": 0, "cost_usd": 0.0}
        for key in order
    }
    for record in records:
        key = _normalized_cost_confidence(record.cost_confidence)
        if key not in rows:
            key = "unknown"
        rows[key]["records"] += 1
        rows[key]["tokens"] += record.total_tokens_including_cached
        rows[key]["cost_usd"] += float(record.estimated_cost_usd or 0.0)
    for event in cost_events:
        key = _normalized_cost_confidence(event.get("cost_confidence"))
        if key not in rows:
            key = "unknown"
        rows[key]["records"] += 1
        rows[key]["tokens"] += _safe_nonnegative_event_int(event, "estimated_input_tokens") + _safe_nonnegative_event_int(event, "estimated_output_tokens")
        rows[key]["cost_usd"] += float(_safe_float(event.get("estimated_cost_usd")) or 0.0)
    return [rows[key] for key in order]


def _usage_confidence_breakdown(records: list[DashboardUsageRecord]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        key = _normalized_usage_confidence(record.usage_confidence)
        row = rows.setdefault(key, {"confidence": key, "records": 0, "tokens": 0})
        row["records"] += 1
        row["tokens"] += record.total_tokens_including_cached
    return sorted(rows.values(), key=lambda row: str(row["confidence"]))


def _usage_over_time_rows(records: list[DashboardUsageRecord]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        timestamp = _usage_record_time(record)
        try:
            date_label = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d") if timestamp > 0 else "Unknown time"
        except (OSError, OverflowError, ValueError):
            # Client-authored timestamps can be absurd-but-finite (ms epoch);
            # one bad row must not 500 the dashboard.
            date_label = "Unknown time"
        row = rows.setdefault(date_label, {"date": date_label, "records": 0, "tokens": 0, "cost_usd": 0.0})
        row["records"] += 1
        row["tokens"] += record.total_tokens_including_cached
        row["cost_usd"] += float(record.estimated_cost_usd or 0.0)
    return sorted(rows.values(), key=lambda row: str(row["date"]), reverse=True)


def _bridge_sections_label(link: dict[str, Any]) -> str:
    sections = link.get("sections")
    if not isinstance(sections, list) or not sections:
        return "No linked sections"
    labels: list[str] = []
    for section in sections[:3]:
        if not isinstance(section, dict):
            continue
        title = section.get("section_title") or section.get("section_id") or "section"
        status = section.get("section_status") or "unknown"
        labels.append(f"{title} ({status})")
    if len(sections) > 3:
        labels.append(f"+{len(sections) - 3} more")
    return "; ".join(labels) or "No linked sections"


def _bridge_usage_debug_label(link: dict[str, Any]) -> str:
    usage_debug = link.get("latest_usage_debug")
    if not isinstance(usage_debug, dict) or not usage_debug:
        return "No usage-debug event"
    basis = usage_debug.get("reporting_basis") or "unknown"
    summary = usage_debug.get("summary")
    if summary:
        return f"{basis}: {summary}"
    return str(basis)


def _bridge_join_status_class(confidence: Any) -> str:
    confidence_label = str(confidence or "")
    if confidence_label in {"exact_client_id", "run_id", "parent_client_id", "exact", "high", "medium"}:
        return "status-ready"
    if confidence_label in {"none", "unjoined"}:
        return "status-missing"
    return "status-needs-import"


def _work_status_class(status: Any) -> str:
    status_label = str(status or "")
    if status_label == "completed":
        return "status-ready"
    if status_label == "blocked":
        return "status-error"
    if status_label == "checkpoint":
        return "status-needs-import"
    return "status-found"


def _evidence_status_class(status: Any) -> str:
    status_label = str(status or "")
    if status_label == "strong":
        return "status-ready"
    if status_label == "failed":
        return "status-error"
    if status_label == "weak":
        return "status-needs-import"
    return "status-missing"


def _ledger_health_status_class(health: Any) -> str:
    health_label = str(health or "")
    if health_label in {"good", "healthy", "running"}:
        return "status-ready"
    if health_label in {"partial", "unknown", "pending", "stopped", "not_configured"}:
        return "status-needs-import"
    if health_label in {"poor", "degraded", "failed", "stale"}:
        return "status-error"
    return "status-missing"


def _evidence_note_for_item(item: dict[str, Any]) -> str:
    events = item.get("evidence_events")
    if isinstance(events, list) and events:
        results = [str(event.get("result") or "unknown") for event in events if isinstance(event, dict)]
        if any(result in {"failed", "error"} for result in results):
            return "Machine check failed or errored"
        if "passed" in results:
            return "Machine check passed"
        return "Evidence recorded"
    files = item.get("files")
    if isinstance(files, list) and files:
        return "Files were named by the agent"
    return "No objective evidence linked"


def _join_state_class(state: Any) -> str:
    state_label = str(state or "")
    if state_label == "attributed":
        return "status-ready"
    if state_label in {"context_matched_unallocated", "ambiguous"}:
        return "status-needs-import"
    if state_label in {"missing_join_keys", "unattributed", "usage_without_mcp_context", "no_usage_found"}:
        return "status-missing"
    return "status-found"


def _join_state_label(state: Any) -> str:
    labels = {
        "attributed": "Attributed",
        "context_matched_unallocated": "Context matched; not allocated",
        "usage_without_mcp_context": "Usage without MCP context",
        "unattributed": "Unattributed",
        "no_usage_found": "No usage found",
        "missing_join_keys": "Missing join keys",
        "ambiguous": "Ambiguous",
    }
    return labels.get(str(state or ""), _display_count_label(state))


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
SESSION_DETAIL_ITEM_LIMIT = 20
WORK_ITEM_DISPLAY_LIMIT = 50
RECONCILIATION_DISPLAY_LIMIT = 120


def _rollup_state_label(state: Any) -> str:
    labels = {
        "attributed": "Attributed",
        "ambiguous": "Ambiguous",
        "context_only": "Context matched; not allocated",
        "unjoined": "No MCP context",
        "sections_only": "Sections recorded; no usage imported",
    }
    return labels.get(str(state or ""), _join_state_label(state))


def _rollup_state_class(state: Any) -> str:
    state_label = str(state or "")
    if state_label == "attributed":
        return "status-ready"
    if state_label in {"ambiguous", "context_only", "sections_only"}:
        return "status-needs-import"
    return "status-missing"


def _severity_status_class(severity: Any) -> str:
    severity_label = str(severity or "")
    if severity_label == "high":
        return "status-error"
    if severity_label == "medium":
        return "status-needs-import"
    return "status-missing"


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


def _session_display_label(client: Any, session_id: Any, session_labels: dict[tuple[str, str], str]) -> str | None:
    """Short display label for a (client, session) pair; NEVER the full id.

    Returns None when there is no session id; pairs missing from the rollup
    label map (id-less bucket edge cases) get an honest placeholder instead
    of a locally re-derived truncation.
    """

    if session_id is None or str(session_id) == "":
        return None
    return session_labels.get((str(client or ""), str(session_id))) or "unlisted session"


def _dominant_confidence_label(breakdown: Any) -> str:
    """The shared dominant-confidence rule (PRD §10.2,
    usage_cube.dominant_cost_confidence) over the ledger's list-shaped
    cost-confidence breakdown — cost-weighted with a row-count fallback, so
    a $-less "unknown" bucket can no longer flip the Overview cost card to
    "mixed" while /tokens and /usage/summary render a single confidence."""

    if not isinstance(breakdown, list) or not breakdown:
        return "unknown confidence"
    costs: dict[str, float] = {}
    counts: dict[str, int] = {}
    for row in breakdown:
        if not isinstance(row, dict):
            continue
        records = _safe_int(row.get("records"))
        cost = _safe_float(row.get("cost_usd")) or 0.0
        if records <= 0 and cost <= 0.0:
            continue
        label = str(row.get("confidence") or "unknown")
        costs[label] = costs.get(label, 0.0) + max(cost, 0.0)
        counts[label] = counts.get(label, 0) + max(records, 0)
    _dominant, _mixed, label = dominant_cost_confidence(costs, counts)
    return label or "unknown confidence"


def _breakdown_map_confidence_label(breakdown: Any) -> str:
    """Same shared dominant-confidence rule for the work ledger's map-shaped
    breakdowns ({confidence: row count}) — row counts are the only weight
    available here, and ties render the mixed label naming both."""

    if not isinstance(breakdown, dict) or not breakdown:
        return "unknown confidence"
    counts = {str(key): _safe_int(value) for key, value in breakdown.items()}
    _dominant, _mixed, label = dominant_cost_confidence({}, counts)
    return label or "unknown confidence"


def _session_goals_label(entry: dict[str, Any]) -> str:
    items = (entry.get("work") or {}).get("items") or []
    titles = [str(item.get("title") or item.get("section_id") or "").strip() for item in items if isinstance(item, dict)]
    titles = [title for title in titles if title]
    if not titles:
        client_title = str(entry.get("client_session_title") or "").strip()
        return _short_text(client_title, max_length=60) if client_title else "—"
    first = _short_text(titles[0], max_length=60)
    if len(titles) == 1:
        return first
    return f"{first} +{len(titles) - 1} more"


def _session_tokens_line(entry: dict[str, Any], esc: Any) -> str:
    """Fresh-tokens segment of the row's muted line 2 (PRD §6.2).

    A session without imported usage rows keeps the honest dash + the
    ledger's ``usage_note`` ("not a zero-cost claim") — the redesign never
    turns an unknown into a zero."""

    usage = entry.get("usage") or {}
    additive_rows = int((usage.get("additive_rows") if "additive_rows" in usage else usage.get("rows")) or 0)
    if not additive_rows:
        note = entry.get("usage_note") or "no usage rows imported for this session id"
        return f'— <span class="note">{esc(note)}</span>'
    excluded_rows = int(usage.get("excluded_non_additive_rows") or 0)
    fresh_label = "known additive fresh subtotal" if excluded_rows else "fresh"
    total_label = "known additive subtotal" if excluded_rows else "total"
    return (
        f"<strong>{esc(_fmt_int(usage.get('fresh_tokens')))}</strong> {esc(fresh_label)} "
        f"<span class=\"note\">(+{esc(_fmt_int(usage.get('cache_creation_tokens')))} cache writes · "
        f"{esc(_fmt_int(usage.get('cache_read_tokens')))} cache reads · "
        f"{esc(_fmt_int(usage.get('total_tokens')))} {esc(total_label)})</span>"
    )


def _session_cost_line(entry: dict[str, Any], esc: Any) -> str:
    """Cost + confidence segment of line 2; '' (segment omitted) when the
    session has no usage rows — the tokens segment already carries the honest
    not-a-zero-cost note, and a bare dash next to it would just repeat it."""

    usage = entry.get("usage") or {}
    additive_rows = int((usage.get("additive_rows") if "additive_rows" in usage else usage.get("rows")) or 0)
    if not additive_rows:
        return ""
    return (
        f"{_fmt_optional_usd(usage.get('estimated_cost_usd'))} "
        f"<span class=\"note\">({esc(_display_count_label(usage.get('cost_confidence')))} cost confidence)</span>"
    )


def _session_children_line(entry: dict[str, Any], esc: Any) -> str:
    related = entry.get("related") or {}
    children_usage = related.get("children_usage")
    if not isinstance(children_usage, dict):
        return ""
    labels = [str(label) for label in (related.get("child_session_labels") or []) if label]
    label_note = ""
    if labels:
        suffix = ", …" if int(related.get("child_session_count") or 0) > len(labels) else ""
        label_note = f" ({', '.join(labels)}{suffix})"
    held_rows = int(children_usage.get("excluded_non_additive_rows") or 0)
    if held_rows and not int(children_usage.get("total_tokens") or 0):
        usage_copy = (
            f"exclusive usage unavailable · {_fmt_int(held_rows)} usage "
            f"row{'s' if held_rows != 1 else ''} held for lineage normalization"
        )
    else:
        usage_copy = (
            f"{_fmt_int(children_usage.get('fresh_tokens'))} fresh / "
            f"{_fmt_int(children_usage.get('total_tokens'))} known total tokens"
            + (
                f" · {_fmt_int(held_rows)} usage row{'s' if held_rows != 1 else ''} held for lineage normalization"
                if held_rows
                else ""
            )
        )
    cost_copy = (
        f"; {_fmt_optional_usd_text(children_usage.get('estimated_cost_usd'))} estimated"
        if children_usage.get("estimated_cost_usd") is not None
        else ""
    )
    return (
        '<div class="session-children"><span class="note">'
        f"{esc(_fmt_int(children_usage.get('sessions')))} subagent session(s) — not allocated to this session: "
        f"{esc(usage_copy)}{esc(cost_copy)}{esc(label_note)}"
        "</span></div>"
    )


def _session_detail_html(entry: dict[str, Any], esc: Any) -> str:
    usage = entry.get("usage") or {}
    join = entry.get("join") or {}
    work = entry.get("work") or {}
    related = entry.get("related") or {}

    lane_lines = []
    for lane in (usage.get("model_lanes") or []):
        if not isinstance(lane, dict):
            continue
        lane_label = str(lane.get("model") or "unknown model")
        if lane.get("lane"):
            lane_label += f" · {lane.get('lane')}"
        lane_lines.append(
            "<li>"
            f"<code>{esc(lane_label)}</code> — {esc(_fmt_int(lane.get('rows')))} row(s) · "
            f"{esc(_fmt_int(lane.get('fresh_tokens')))} fresh · +{esc(_fmt_int(lane.get('cache_creation_tokens')))} cache writes · "
            f"{esc(_fmt_int(lane.get('cache_read_tokens')))} cache reads · {esc(_fmt_int(lane.get('total_tokens')))} total · "
            f"{esc(_fmt_optional_usd_text(lane.get('estimated_cost_usd')))}"
            "</li>"
        )
    lanes_html = "".join(lane_lines) or "<li>No imported usage rows for this session id.</li>"

    work_items = [item for item in (work.get("items") or []) if isinstance(item, dict)]
    section_capped = capped_rows(work_items, SESSION_DETAIL_ITEM_LIMIT)
    section_lines = []
    for item in section_capped["rows"]:
        conflict_chip = ""
        if item.get("log_evidence_conflict"):
            # Id-free by the Phase 2 reason-string rule; the full claimed ids
            # stay JSON-only on the ledger work_items. The chip names the key
            # that actually conflicted (session / transcript / client) so a
            # transcript-only conflict is not mislabelled as a session-id one.
            phrase = _log_evidence_conflict_phrase({"conflicting_keys": item.get("log_evidence_conflict_keys") or []})
            conflict_chip = (
                f' <span class="status status-error" title="{esc(phrase)}; '
                'conflicting evidence vetoes joins">id conflict</span>'
            )
        section_lines.append(
            "<li>"
            f"<strong>{esc(item.get('title') or item.get('section_id'))}</strong> "
            f"<span class=\"status {_work_status_class(item.get('latest_status'))}\">{esc(item.get('latest_status'))}</span> "
            f"<span class=\"status {_evidence_status_class(item.get('evidence_status'))}\">{esc(item.get('evidence_status'))} evidence</span>"
            f"{conflict_chip}"
            "</li>"
        )
    sections_html = "".join(section_lines) or "<li>No MCP sections recorded in this session.</li>"
    section_cap_note = _cap_note(section_capped["shown"], section_capped["total"], "sections")

    attributed_lines = []
    for attributed in (join.get("attributed_work") or []):
        if not isinstance(attributed, dict):
            continue
        attributed_lines.append(
            "<li>"
            f"<strong>{esc(attributed.get('title') or attributed.get('section_id'))}</strong> — "
            f"{esc(_display_count_label(attributed.get('join_strategy')))} at "
            f"{esc(_display_count_label(attributed.get('join_confidence')))} confidence"
            "</li>"
        )
    attributed_html = "".join(attributed_lines) or "<li>No usage allocated to specific work in this session.</li>"
    ambiguous_count = len(join.get("ambiguous_candidate_work_ids") or [])
    ambiguous_note = ""
    if ambiguous_count:
        ambiguous_note = (
            f'<p class="note">{esc(_fmt_int(ambiguous_count))} candidate work item(s) share this session\'s '
            "context; agentacct does not allocate ambiguous usage.</p>"
        )

    # Each line below is already escaped where dynamic; joined without a
    # second escape pass (a blanket esc() here would re-escape the <code>
    # markup around the parent label).
    related_lines = []
    parent = related.get("parent")
    if isinstance(parent, dict):
        related_lines.append(
            f"child of session <code>{esc(parent.get('label') or 'unknown')}</code> (client-reported parent id)"
        )
    if related.get("note"):
        related_lines.append(esc(related.get("note")))
    if isinstance(related.get("children_usage"), dict):
        related_lines.append(
            "descendant usage above is the children's OWN usage — shown separately, never combined into this session"
        )
    related_html = ""
    if related_lines:
        related_html = f'<p class="note">Related sessions: {"; ".join(related_lines)}</p>'

    return (
        '<div class="session-detail">'
        '<div class="detail-title">Per-model usage</div>'
        f"<ul>{lanes_html}</ul>"
        '<div class="detail-title">Sections</div>'
        f"{section_cap_note}"
        f"<ul>{sections_html}</ul>"
        '<div class="detail-title">Attributed work</div>'
        f"<ul>{attributed_html}</ul>"
        f"{ambiguous_note}"
        f"{related_html}"
        "</div>"
    )


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


def _log_evidence_conflict_phrase(conflict: Any) -> str:
    """Id-free clause naming the key that conflicts with the client's own log.

    Reads the ``conflicting_keys`` recorded by log_evidence.py; falls back to
    the session wording for a truthy-but-shapeless marker. Renders in an HTML
    title attr, so it carries no ids.
    """

    keys = conflict.get("conflicting_keys") if isinstance(conflict, dict) else None
    key = "client_session_id"
    if isinstance(keys, (list, tuple)):
        for candidate in ("client_session_id", "client_transcript_id", "client"):
            if candidate in keys:
                key = candidate
                break
    if key == "client_transcript_id":
        return "the section's agent-claimed transcript id conflicts with the transcript evidenced by the client's own log"
    if key == "client":
        return "the section's agent-claimed client conflicts with the client evidenced by its own log"
    return "the section's agent-claimed session id conflicts with the session evidenced by the client's own log"


def _append_sentence(base: str, appended: str | None) -> str:
    """Join an override note and a base rollup reason into well-formed prose.

    The override note (``base``) is emitted as a complete sentence; the base
    rollup ``reason`` — a lowercase clause like "no section shared ..." or a
    veto reason — is appended as its OWN sentence: the base is given terminal
    punctuation and the appended clause is capitalized and given its own. This
    avoids the run-on titles produced by bare " " concatenation.
    """

    base = base.strip()
    if base and base[-1] not in ".!?":
        base += "."
    appended = (appended or "").strip()
    if not appended:
        return base
    appended = appended[0].upper() + appended[1:]
    if appended[-1] not in ".!?":
        appended += "."
    return f"{base} {appended}" if base else appended


def _session_join_chip_html(entry: dict[str, Any], esc: Any) -> str:
    """Join chip + per-session coverage. Never implies an unmade allocation.

    - 'Attributed' renders ONLY when ALL usage rows in the session are
      attributed. A mixed session renders 'Partially attributed' with a
      visible coverage line ('N of M rows · X fresh tokens attributed'), so
      the whole-session tokens/cost figures (exact-key session truth) can
      never be read as the amount the canonical ledger allocated.
    - A canonically unjoined session that still has recorded sections renders
      'Not attributed' (the veto/no-match reason travels on the title attr);
      the chip never claims a match the matcher refused.
    - Unjoined-chip precedence (top wins), each claim only what its data
      proves: client-log-evidenced context recorded IN this session ->
      pre-instrumentation (recording was not installed when the session ran,
      so context was impossible — neutral, not a warning) -> other_project
      (context, if any, lives in that project's own store) -> project-level
      context only (codex cannot self-identify) -> bare 'No MCP context'.
      None of them ever implies an allocation. "unknown" instrumentation
      state renders exactly as before — only a marker-proven pre state may
      change the chip.
    - The pre-instrumentation chip may fire ONLY for a truly context-free
      session: zero sections, zero conflict-vetoed rows, zero evidenced
      events, zero evidence conflicts. A session that recorded ANYTHING here
      must keep its honest chip ('Not attributed', the veto reason) — "no MCP
      work context could have been recorded" would be a false claim.
    """

    join = entry.get("join") or {}
    state = str(join.get("state") or "")
    reason = str(join.get("reason") or "")
    row_states = join.get("row_states") if isinstance(join.get("row_states"), dict) else {}
    total_rows = sum(int(value or 0) for value in row_states.values())
    label = _rollup_state_label(state)
    status_class = _rollup_state_class(state)
    coverage_note = ""
    evidence = join.get("client_log_evidence") if isinstance(join.get("client_log_evidence"), dict) else {}
    evidenced_count = int(evidence.get("evidenced_event_count") or 0)
    unjoined_override = False
    if state == "unjoined" and evidenced_count > 0:
        unjoined_override = True
        label = "MCP context recorded in this session (client-log evidence); not allocated"
        status_class = "status-needs-import"
        pairing_note = (
            f"agentacct paired {evidenced_count} recorded event(s) with this session's own client log at import "
            "time. Context grouping only — no usage allocation."
        )
        if int(evidence.get("conflicts") or 0) > 0:
            # Key-neutral (the block carries only a count, not which id
            # conflicted); the per-key wording lives on the section detail
            # chip and the veto reason below.
            pairing_note += " An agent-claimed context id conflicted with the log evidence."
        reason = _append_sentence(pairing_note, reason)
    elif (
        state == "unjoined"
        and str(entry.get("instrumentation_state") or "") == "pre_instrumentation"
        and int(((entry.get("work") or {}).get("counts") or {}).get("total") or 0) == 0
        and int(join.get("vetoed_rows") or 0) == 0
        and evidenced_count == 0
        and int(evidence.get("conflicts") or 0) == 0
    ):
        unjoined_override = True
        label = "Pre-instrumentation — recording was not installed when this session ran"
        # Neutral chip, not the missing-context gray: absent context is the
        # EXPECTED state for a pre-install session, not a gap to fix.
        status_class = "status-found"
        installed_date = _fmt_date(entry.get("instrumentation_installed_at"))
        if str(entry.get("instrumentation_state_basis") or "") == "inherited_from_root":
            # A child can START after install yet inherit pre from its root:
            # never claim THIS session started before the marker.
            explanation = (
                f"The root session of this subagent session started before recording instructions for "
                f"{_human_client(entry.get('client'))} were installed {installed_date}."
            )
        else:
            explanation = (
                f"Recording instructions for {_human_client(entry.get('client'))} were installed {installed_date}; "
                "this session started before that, so no MCP work context could have been recorded."
            )
        reason = _append_sentence(explanation, reason)
    elif state == "unjoined" and join.get("context_scope") == "other_project" and entry.get("project"):
        unjoined_override = True
        project = str(entry.get("project"))
        label = f"No MCP context in this store — session ran in {project}"
        reason = _append_sentence(
            f"Stores are per-project: MCP context recorded in {project} (if any) lives in that project's own store, "
            "not this one.",
            reason,
        )
    elif state == "unjoined" and join.get("project_level_context_only"):
        unjoined_override = True
        label = "Project-level MCP context only (Codex sessions cannot self-identify)"
        reason = _append_sentence(
            "This store has MCP work context recorded from this project, but Codex sessions cannot pass their own "
            "session id, so none of it can be tied to this specific session. No usage is allocated.",
            reason,
        )
    if state == "attributed":
        attributed_rows = int(row_states.get("attributed") or 0)
        if total_rows and attributed_rows < total_rows:
            label = "Partially attributed"
            status_class = "status-needs-import"
            coverage_note = (
                f'<br><span class="note">{esc(_fmt_int(attributed_rows))} of {esc(_fmt_int(total_rows))} rows · '
                f"{esc(_fmt_int(join.get('attributed_fresh_tokens')))} fresh tokens attributed</span>"
            )
            reason = f"attributed row(s) only: {reason}" if reason else ""
    elif state == "unjoined" and not unjoined_override and int(((entry.get("work") or {}).get("counts") or {}).get("total") or 0) > 0:
        # Sections exist but every row is canonically unjoined (e.g. a
        # transcript-conflict veto): claim nothing.
        label = "Not attributed"
    return (
        f'<span class="status {status_class}" title="{esc(reason)}">{esc(label)}</span>{coverage_note}'
    )


def _session_row_html(entry: dict[str, Any], esc: Any) -> str:
    """One session row (PRD §6.2) — the SAME component everywhere it appears
    (the /sessions explorer and the Overview 10-row preview). Still a native
    <details>, no JS.

    Line 1: lane-colored client badge · 8-char id · kind/lineage chips ·
    goal (first section title) · join chip. A goal-less session renders no
    goal text — the join chip already IS the honest state chip (No MCP
    context / Pre-instrumentation / other_project…, exact
    _session_join_chip_html semantics), so the label is never duplicated.

    Line 2 (muted): project · started (local, first_activity_at) · duration
    (humanized h/m) · turns · fresh tokens (+cache note) · cost +
    confidence. Duration and turns are OMITTED when the ledger has None —
    an absent value never renders as a guessed 0."""

    kind = entry.get("session_kind")
    # "internal" alone reads like a agentacct concept; name the actual thing
    # (client-spawned auto-review) so the row explains itself.
    kind_label = "internal (auto-review)" if str(kind or "") == "internal" else kind
    kind_chip = f'<span class="chip">{esc(kind_label)} session</span>' if kind else ""
    related = entry.get("related") or {}
    parent = related.get("parent")
    parent_chip = ""
    if isinstance(parent, dict) and parent.get("label"):
        # Rendered whenever a top-level row has a client-reported parent
        # (deep lineage, orphaned children): the lineage stays visible even
        # though the row could not nest under a rendered parent row.
        parent_chip = f'<span class="chip">child of {esc(parent.get("label"))}</span>'
    project = entry.get("project")
    project_html = esc(project) if project else '<span class="note">no project label</span>'
    if project and entry.get("project_source") == "claude_worktree":
        # The label is the OWNING repo (read-time remap); the chip preserves
        # the ran-in-a-temporary-worktree fact without the meaningless
        # worktree folder name. JSON keeps the clean name + project_source.
        # The title names the OWNER LABEL, never "this repository" — on this
        # dashboard the row may belong to another project's worktree.
        project_html += (
            f' <span class="chip" title="session ran in a temporary Claude worktree of {esc(project)}">'
            "worktree</span>"
        )
    goal_label = _session_goals_label(entry)
    goal_html = f'<span class="session-goal">{esc(goal_label)}</span>' if goal_label != "—" else ""
    started = _fmt_time(entry.get("first_activity_at"))
    started_html = f"started {esc(started)}" if started else '<span class="note">no activity time</span>'
    duration_label = _fmt_duration_hm(entry.get("duration_seconds"))
    turns_total = (entry.get("usage") or {}).get("turns_total")

    line1_parts = [
        f'<span class="badge-client {client_lane_class(entry.get("client"))}">{esc(_human_client(entry.get("client")))}</span>',
        f'<code>{esc(entry.get("client_session_id_short"))}</code>',
    ]
    if kind_chip:
        line1_parts.append(kind_chip)
    if parent_chip:
        line1_parts.append(parent_chip)
    if goal_html:
        line1_parts.append(goal_html)
    line1_parts.append(_session_join_chip_html(entry, esc))

    meta_segments = [project_html, started_html]
    if duration_label:
        meta_segments.append(esc(duration_label))
    if turns_total is not None:
        meta_segments.append(f"{esc(_fmt_int(turns_total))} turn(s)")
    meta_segments.append(_session_tokens_line(entry, esc))
    cost_line = _session_cost_line(entry, esc)
    if cost_line:
        meta_segments.append(cost_line)

    return (
        '<details class="session-row">'
        "<summary>"
        f'<div class="session-line">{" ".join(line1_parts)}</div>'
        f'<div class="session-line session-meta">{" · ".join(meta_segments)}</div>'
        f"{_session_children_line(entry, esc)}"
        "</summary>"
        f"{_session_detail_html(entry, esc)}"
        "</details>"
    )


def _work_item_row_html(item: dict[str, Any], esc: Any, session_labels: dict[tuple[str, str], str]) -> str:
    explanation = item.get("join_explanation") if isinstance(item.get("join_explanation"), dict) else {}
    state = explanation.get("usage_join_state")
    linked = int(item.get("linked_usage_records") or 0)
    if linked > 0:
        usage_cell = (
            f"<strong>{esc(_fmt_int(item.get('usage_fresh_total')))}</strong> fresh tokens"
            f"<br><span class=\"note\">{esc(_fmt_int(item.get('usage_total')))} total incl. caches · "
            f"{esc(_fmt_int(linked))} usage row(s) · {esc(_fmt_optional_usd_text(item.get('estimated_cost_total')))} "
            f"({esc(_breakdown_map_confidence_label(item.get('cost_confidence_breakdown')))})</span>"
        )
    else:
        usage_cell = '—<br><span class="note">Usage unknown / not attributed</span>'
    session_label = _session_display_label(item.get("client"), item.get("client_session_id"), session_labels)
    session_note = f"session <code>{esc(session_label)}</code>" if session_label else "no session id"
    next_step = item.get("next_step") or item.get("blocker") or explanation.get("recommended_next_step")
    next_step_note = f'<br><span class="note">Next: {esc(next_step)}</span>' if next_step else ""
    href = "/work-items/" + quote(str(item.get("work_id") or item.get("section_id") or ""), safe="")
    return (
        "<tr>"
        f"<td><strong>{esc(item.get('title'))}</strong><br>"
        f'<span class="note">{esc(_human_client(item.get("client")))} · {session_note}</span>{next_step_note}</td>'
        f"<td><span class=\"status {_work_status_class(item.get('latest_status'))}\">{esc(item.get('latest_status'))}</span><br>"
        f"<span class=\"note\">{esc(_fmt_time(item.get('updated_at')))}</span></td>"
        f"<td><span class=\"status {_evidence_status_class(item.get('evidence_status'))}\">{esc(item.get('evidence_status'))}</span><br>"
        f"<span class=\"note\">{esc(_evidence_note_for_item(item))}</span></td>"
        f"<td>{usage_cell}</td>"
        f"<td><span class=\"status {_join_state_class(state)}\" title=\"{esc(explanation.get('join_reason') or '')}\">{esc(_join_state_label(state))}</span><br>"
        f"<span class=\"note\">{esc(_display_count_label(explanation.get('join_confidence')))} confidence</span></td>"
        f'<td><a href="{esc(href)}">JSON</a></td>'
        "</tr>"
    )


def _attention_group_row_html(group: dict[str, Any], esc: Any) -> str:
    severity = str(group.get("severity") or "medium")
    example_lines = "".join(
        f"<br><span class=\"note\">e.g. {esc(example.get('label'))}</span>"
        for example in (group.get("example_refs") or [])[:3]
        if isinstance(example, dict) and example.get("label")
    )
    tokens_note = ""
    if group.get("fresh_tokens") is not None:
        tokens_note = (
            f"<br><span class=\"note\">{esc(_fmt_int(group.get('fresh_tokens')))} fresh tokens across these rows "
            f"({esc(_fmt_int(group.get('total_tokens')))} incl. caches)</span>"
        )
    return (
        "<tr>"
        f"<td><span class=\"status {_severity_status_class(severity)}\">{esc(_display_count_label(severity))}</span></td>"
        f"<td><strong>{esc(group.get('title'))}</strong>{tokens_note}{example_lines}</td>"
        f"<td>{esc(_fmt_int(group.get('count')))}</td>"
        f"<td>{esc(group.get('recommended_next_step') or '')}</td>"
        "</tr>"
    )


def _bridge_join_keys_label(link: dict[str, Any]) -> str:
    keys = link.get("join_keys")
    if not isinstance(keys, list) or not keys:
        return "No matching keys"
    return ", ".join(str(key) for key in keys)


def _bridge_context_counts_label(link: dict[str, Any]) -> str:
    return (
        f"{_fmt_int(link.get('client_context_count'))} client context / "
        f"{_fmt_int(link.get('section_count'))} sections / "
        f"{_fmt_int(link.get('usage_debug_count'))} usage-debug"
    )


def _bridge_semantic_event_label(context: dict[str, Any]) -> str:
    kind = str(context.get("semantic_kind") or "context")
    event_type = str(context.get("event_type") or "")
    if event_type:
        return f"{kind} / {event_type}"
    return kind


def _cost_breakdown_note(row: dict[str, Any], key: str) -> str:
    if int(row.get("priced_records") or 0) <= 0:
        return "No estimate"
    return _fmt_usd(row.get(key))


def _cost_coverage_label(row: dict[str, Any]) -> str:
    priced = int(row.get("priced_records") or 0)
    unpriced = int(row.get("unpriced_records") or 0)
    if unpriced <= 0:
        return f"{_fmt_int(priced)} priced"
    if priced <= 0:
        return f"{_fmt_int(unpriced)} unpriced"
    return f"{_fmt_int(priced)} priced / {_fmt_int(unpriced)} unpriced"


def _local_cost_cell(local_records: list[DashboardUsageRecord], esc: Any) -> str:
    if not local_records:
        return '<span class="note">No usage rows</span>'
    client_cost = sum(float(record.client_reported_cost_usd or 0.0) for record in local_records)
    estimated_cost = 0.0
    priced = 0
    for record in local_records:
        if record.estimated_cost_usd is None:
            continue
        estimated_cost += record.estimated_cost_usd
        priced += 1
    if client_cost > 0:
        return f"{_fmt_usd(client_cost)}<br><span class=\"note\">client-reported</span>"
    if priced > 0:
        return f"{_fmt_usd(estimated_cost)}<br><span class=\"note\">Token-price estimate; fast/cache rules when known</span>"
    return '<span class="note">No estimate yet</span>'


def _local_models_cell(local_records: list[DashboardUsageRecord], esc: Any) -> str:
    seen: list[str] = []
    for record in local_records:
        label = _display_provider_model(record.provider, record.model)
        if label == "Not reported" or label in seen:
            continue
        seen.append(label)
    if not seen:
        return '<span class="note">Not reported</span>'
    visible = seen[:3]
    extra = len(seen) - len(visible)
    suffix = f" + {extra} more" if extra > 0 else ""
    return esc(", ".join(visible) + suffix)


def _client_models_cell(models: Any, esc: Any) -> str:
    """Model list cell with a '+N more' suffix instead of a silent [:3] cut."""

    if not isinstance(models, list) or not models:
        return ""
    visible = [str(model) for model in models[:3]]
    extra = len(models) - len(visible)
    suffix = f" + {extra} more" if extra > 0 else ""
    return esc(", ".join(visible) + suffix)


def _source_status_badge(source: UsageSourceDiscovery, local_records: list[DashboardUsageRecord]) -> str:
    if source.status == "error":
        return '<span class="status status-error">Read error</span>'
    if source.status != "found":
        return '<span class="status status-missing">Not found</span>'
    if source.client == "cursor":
        return '<span class="status status-ready">Session observations only</span>'
    if local_records:
        return '<span class="status status-ready">Detected</span>'
    if source.importer:
        return '<span class="status status-needs-import">Found, no tokens yet</span>'
    return '<span class="status status-missing">Unsupported store</span>'


def _source_notes_html(source: UsageSourceDiscovery, esc: Any) -> str:
    return (
        f'<br><span class="note">{esc("; ".join(source.notes))}</span>'
        if source.notes
        else ""
    )


def _local_usage_cell(local_records: list[DashboardUsageRecord], esc: Any) -> str:
    if not local_records:
        return '<span class="note">No token rows in preview</span>'
    totals = _usage_record_totals(local_records)
    return (
        f"{esc(_fmt_int(totals['total_tokens_including_cached']))} total"
        f"<br><span class=\"note\">{esc(_fmt_int(totals['input_tokens']))} input / "
        f"{esc(_fmt_int(totals['output_tokens']))} output / "
        f"{esc(_fmt_int(totals['cache_creation_input_tokens']))} cache create / "
        f"{esc(_fmt_int(totals['cache_read_input_tokens']))} cache read</span>"
    )


def _confidence_cell(source: UsageSourceDiscovery, local_records: list[DashboardUsageRecord], esc: Any) -> str:
    if source.client == "cursor" and not local_records:
        return "Usage: unavailable by design<br><span class=\"note\">Cost: unavailable by design</span>"
    has_client_cost = any(record.client_reported_cost_usd is not None for record in local_records) or source.cost_confidence == "client_reported"
    has_estimated_cost = any(record.estimated_cost_usd is not None for record in local_records)
    usage_label = "client-reported" if source.usage_confidence == "client_reported" or local_records else "not found yet"
    if has_client_cost:
        cost_label = "client-reported"
    elif has_estimated_cost:
        cost_label = "estimated from tokens"
    else:
        cost_label = "not found yet"
    return f"Usage: {esc(usage_label)}<br><span class=\"note\">Cost: {esc(cost_label)}</span>"


def _sort_control(*, current: str, options: list[tuple[str, str]], param: str, extra: dict[str, str], esc: Any, base: str = "/raw") -> str:
    items = []
    for value, label in options:
        params = {**extra, param: value}
        href = f"{base}?" + urlencode(params)
        active = " active" if value == current else ""
        items.append(f"<a class=\"sort-link{active}\" href=\"{esc(href)}\">{esc(label)}</a>")
    return " ".join(items)


def _safe_choice(value: str, allowed: set[str], default: str) -> str:
    return value if value in allowed else default


def _accepts_html(accept_header: str | None) -> bool:
    """Accept negotiation for the dual HTML/JSON /sessions path (the
    realistic RFC 9110 §12.5.1 subset, q-values included).

    HTML is served only when the client names an HTML type (``text/html`` or
    ``text/*``) with a nonzero q that beats any explicit
    ``application/json`` preference. A bare ``*/*`` (curl, python-requests,
    fetch defaults), an absent/empty header, or a JSON-preferring header
    stays JSON — and ``text/html;q=0`` is an explicit refusal, never HTML."""

    if not accept_header:
        return False
    html_q: float | None = None
    json_q: float | None = None
    for item in accept_header.split(","):
        parts = item.strip().split(";")
        media = parts[0].strip().lower()
        if not media:
            continue
        q = 1.0
        for param in parts[1:]:
            name, _, value = param.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 0.0
                break
        if media in {"text/html", "text/*"}:
            html_q = q if html_q is None else max(html_q, q)
        elif media in {"application/json", "application/*"}:
            json_q = q if json_q is None else max(json_q, q)
    if html_q is None or html_q <= 0.0:
        return False
    return json_q is None or html_q > json_q


TIMELINE_VIEWS = {"grouped", "all", "mcp", "usage"}
TIMELINE_ROW_LIMIT = 80


# Grouped-view collapsing rules per usage-bearing event kind: proxy and
# diagnostic rows group exactly like import rows (unbounded row-level
# passthrough is how sections got drowned), just labeled distinctly.
_TIMELINE_GROUP_KINDS = {
    "usage": {
        "group_kind": "usage_group",
        "source": "log_import",
        "source_type": "log_import",
        "title": lambda client, count: f"{_human_client(client)} usage — {_fmt_int(count)} rows",
    },
    "proxy_usage": {
        "group_kind": "proxy_usage_group",
        "source": "proxy",
        "source_type": "proxy",
        "title": lambda client, count: f"Proxy usage — {_fmt_int(count)} budget decision(s)",
    },
    "usage_diagnostic": {
        "group_kind": "usage_diagnostic_group",
        "source": "diagnostic",
        "source_type": "diagnostic",
        "title": lambda client, count: f"{_human_client(client)} usage diagnostic — {_fmt_int(count)} event(s)",
    },
}


def _grouped_timeline_entries(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Display-level grouping for the raw Work Timeline (view=grouped).

    Collapses the usage-bearing kinds (``usage``, ``proxy_usage``,
    ``usage_diagnostic``) into ONE synthetic entry per (kind, client/source,
    local calendar day); sections, evidence, and usage-debug rows pass
    through row-level, so MCP sections stop being drowned by ANY unbounded
    usage-row class (import rows and proxy budget decisions alike). Pure
    display shaping over ledger["timeline"] — the ledger and the /timeline
    JSON are untouched.

    Display honesty: group rows carry exact sums and attributed vs
    not-attributed COUNTS only (a member counts as attributed exactly when
    the canonical ledger allocated it: work_id present and join_confidence
    not "unjoined") — never a work title, so the grouped view can never imply
    an allocation the ledger did not make. No session ids and no paths appear
    on group rows. Grouping uses the local calendar day (same convention as
    _usage_over_time_rows); a session spanning midnight splits across two
    group rows.
    """

    passthrough: list[dict[str, Any]] = []
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in timeline:
        kind = str(entry.get("event_kind") or "")
        if kind not in _TIMELINE_GROUP_KINDS:
            passthrough.append(entry)
            continue
        timestamp = _safe_float(entry.get("time")) or 0.0
        try:
            day = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d") if timestamp > 0 else "unknown"
        except (OSError, OverflowError, ValueError):
            # Absurd-but-finite timestamps must not crash the default view;
            # the row still groups (and sorts) under an unknown day.
            day = "unknown"
        client = str(entry.get("client") or entry.get("source") or "unknown")
        group = groups.setdefault(
            (kind, client, day),
            {
                "kind": kind,
                "client": client,
                "time": 0.0,
                "rows": 0,
                "attributed_rows": 0,
                "fresh_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": None,
            },
        )
        group["time"] = max(float(group["time"]), timestamp)
        group["rows"] += 1
        # Same rule as the ledger's attributed predicate: the decision
        # allocated iff it carries a work_id at a joined confidence.
        if entry.get("work_id") and str(entry.get("join_confidence") or "unjoined") != "unjoined":
            group["attributed_rows"] += 1
        group["fresh_tokens"] += int(entry.get("tokens_fresh") or 0)
        group["cache_creation_tokens"] += int(entry.get("tokens_cache_creation") or 0)
        group["cache_read_tokens"] += int(entry.get("tokens_cache_read") or 0)
        group["total_tokens"] += int(entry.get("tokens") or 0)
        cost = _safe_float(entry.get("estimated_cost_usd"))
        if cost is not None:
            group["estimated_cost_usd"] = float(group["estimated_cost_usd"] or 0.0) + cost

    synthetic: list[dict[str, Any]] = []
    for group in groups.values():
        rules = _TIMELINE_GROUP_KINDS[str(group["kind"])]
        unattributed_rows = int(group["rows"]) - int(group["attributed_rows"])
        cost_label = _fmt_usd(group["estimated_cost_usd"]) if group["estimated_cost_usd"] is not None else "No cost estimate"
        synthetic.append(
            {
                "time": group["time"] or None,
                "source": rules["source"],
                "source_type": rules["source_type"],
                "event_kind": rules["group_kind"],
                "title": rules["title"](group["client"], group["rows"]),
                "detail": (
                    f"{_fmt_int(group['total_tokens'])} tokens incl. cache reads "
                    f"({_fmt_int(group['fresh_tokens'])} fresh / {_fmt_int(group['cache_creation_tokens'])} cache write / "
                    f"{_fmt_int(group['cache_read_tokens'])} cache read); {cost_label}"
                ),
                # Fresh tokens are the group headline (locked decision); the
                # renderer labels the figure "fresh" so it cannot be misread
                # as the raw rows' everything-included total.
                "tokens": group["fresh_tokens"],
                "tokens_fresh": group["fresh_tokens"],
                "tokens_cache_read": group["cache_read_tokens"],
                "tokens_cache_creation": group["cache_creation_tokens"],
                "estimated_cost_usd": group["estimated_cost_usd"],
                "status": "grouped by client/day",
                "work_id": None,
                "join_confidence": f"{_fmt_int(group['attributed_rows'])} attributed / {_fmt_int(unattributed_rows)} not attributed",
            }
        )
    return sorted(passthrough + synthetic, key=lambda entry: float(entry.get("time") or 0.0), reverse=True)


def _timeline_entries_for_view(timeline: list[dict[str, Any]], timeline_view: str) -> list[dict[str, Any]]:
    if timeline_view == "grouped":
        return _grouped_timeline_entries(timeline)
    if timeline_view == "mcp":
        return [entry for entry in timeline if entry.get("event_kind") in {"work", "evidence", "usage_debug"}]
    if timeline_view == "usage":
        return [entry for entry in timeline if entry.get("event_kind") in {"usage", "proxy_usage", "usage_diagnostic"}]
    return timeline


def _timeline_source_type_class(value: Any) -> str:
    source_type = str(value or "")
    if source_type == "log_import":
        return "status-ready"
    if source_type == "mcp_agent_reported":
        return "status-found"
    if source_type == "proxy":
        return "status-needs-import"
    return "status-missing"


def _timeline_tokens_cell(entry: dict[str, Any]) -> str:
    if entry.get("tokens") is None:
        return ""
    if str(entry.get("event_kind") or "").endswith("_group"):
        return f"{_fmt_int(entry.get('tokens'))} fresh"
    return _fmt_int(entry.get("tokens"))


def _timeline_status_cell(entry: dict[str, Any]) -> str:
    if entry.get("status"):
        return str(entry.get("status"))
    if entry.get("event_kind") in {"usage", "proxy_usage", "usage_diagnostic"}:
        return (
            f"{entry.get('usage_confidence') or 'unknown'} usage / "
            f"{entry.get('cost_confidence') or 'unknown'} cost"
        )
    return ""


def _timeline_row(entry: dict[str, Any], esc: Any) -> str:
    return (
        "<tr>"
        f"<td>{esc(_fmt_time(entry.get('time')))}</td>"
        f"<td><span class=\"status {_timeline_source_type_class(entry.get('source_type'))}\">{esc(entry.get('source_type'))}</span></td>"
        f"<td><strong>{esc(entry.get('title'))}</strong><br><span class=\"note\">{esc(entry.get('detail'))}</span></td>"
        f"<td>{esc(_timeline_tokens_cell(entry))}</td>"
        f"<td>{_fmt_timeline_cost_cell(entry.get('estimated_cost_usd'))}</td>"
        f"<td>{esc(_timeline_status_cell(entry))}</td>"
        f"<td>{esc(entry.get('join_confidence'))}</td>"
        "</tr>"
    )


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

DASHBOARD_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"

OVERVIEW_SESSION_PREVIEW_LIMIT = 10

# Primary top nav: the product surface only. Control (governs agentacct-owned
# launched processes) and Advanced (forensic evidence tools) are demoted to a
# subtle footer entry — their routes stay fully reachable, they just no longer
# clutter the primary nav for the common observe-only flow.
_DASHBOARD_PAGES = (
    ("overview", "/", "Work"),
    ("tokens", "/tokens", "Usage"),
)
# Secondary surfaces, reachable via a demoted footer link (not the primary nav).
_DASHBOARD_SECONDARY_PAGES = (
    ("/advanced", "Advanced & evidence"),
    ("/control", "Control"),
)

# Product navigation names user goals, not internal projections.  Legacy and
# forensic routes remain stable, but highlight the product area that owns
# them instead of becoming eight peer destinations in the primary nav.
_DASHBOARD_PAGE_GROUPS = {
    "overview": "overview",
    "sessions": "overview",
    "control": "control",
    "tokens": "tokens",
    "advanced": "advanced",
    "raw": "advanced",
    "work-graph": "advanced",
    "evidence-matrix": "advanced",
    "discrepancies": "advanced",
    "cost-outcome-basis": "advanced",
}

_DASHBOARD_PAGE_HEADERS = {
    "overview": ("Work", "What your agents are doing, what finished, and when they need you."),
    "sessions": ("All activity", "Explore recorded sessions, named work, and attribution details."),
    "control": ("Control", "Plan and govern only the local execution processes agentacct itself owns."),
    "tokens": ("Usage", "Understand token usage and estimated cost from saved local activity."),
    "advanced": ("Advanced", "Inspect data coverage, evidence projections, and local diagnostics."),
    "raw": ("Local logs", "Preview and inspect the local records behind agentacct's product views."),
    "work-graph": ("Work graph", "Inspect how normalized evidence connects to recorded work."),
    "evidence-matrix": ("Evidence matrix", "Compare source authority and measurement basis by dimension."),
    "discrepancies": ("Discrepancies", "Review evidence conflicts and coverage gaps preserved by agentacct."),
    "cost-outcome-basis": ("Cost and outcome basis", "Inspect the evidence basis behind cost and outcome claims."),
}

# PRD §5.1 / §11.3 (locked): the cost-basis disclosure that travels with the
# Est. cost co-headline on the Overview and the /tokens page header.
COST_BASIS_DISCLOSURE = (
    "Costs are estimates derived from token counts × known API prices "
    "(client_reported when the client itself reported cost) — never provider invoices."
)

# Design tokens (PRD §7): spacing/type scale, status colors lifted verbatim
# from the existing chips, and the 6-color platform lane palette (5 known
# clients + other) shared by badges/bars and the Phase 3.5b SVG chart. Light
# theme only — dark mode is deferred past Phase 3.5 (locked decision Q4).
_DASHBOARD_STYLE = """    :root {
      --font-2xs: 0.78rem; --font-xs: 0.8rem; --font-sm: 0.82rem; --font-chip: 0.85rem;
      --font-note: 0.88rem; --font-body: 0.9rem; --font-nav: 0.95rem;
      --font-h2: 1.15rem; --font-stat: 1.2rem; --font-value: 1.55rem; --font-h1: 2rem;
      --font-mono: ui-monospace, "SF Mono", "JetBrains Mono", "Menlo", "Consolas", monospace;
      --space-1: 0.2rem; --space-2: 0.35rem; --space-3: 0.5rem; --space-4: 0.65rem;
      --space-5: 0.75rem; --space-6: 1rem; --space-7: 1.5rem;
      --radius-sm: 7px; --radius-md: 9px; --radius-lg: 12px; --radius-pill: 999px;
      --color-bg: #f4f6f9; --color-surface: #ffffff; --color-surface-muted: #f1f4f8;
      --color-border: #e4e8ef; --color-border-strong: #d3dae4;
      --color-text: #0f1622; --color-text-secondary: #334155;
      --color-text-muted: #55606f; --color-text-faint: #8590a0; --color-muted: #55606f;
      --color-accent: #4f46e5; --color-cost: #059669; --color-warn: #b45309;
      --color-log: #7c3aed; --color-bridge: #0e7490;
      --status-ok-bg: #dcfce7; --status-ok-fg: #166534;
      --status-warn-bg: #fef3c7; --status-warn-fg: #92400e;
      --status-muted-bg: #eef1f6; --status-muted-fg: #55606f;
      --status-error-bg: #fee2e2; --status-error-fg: #991b1b;
      --badge-bg: #eef2ff; --badge-fg: #3730a3;
      --notice-bg: #ecfdf5; --notice-border: #a7f3d0; --notice-fg: #065f46;
      --alert-bg: #fef3c7; --alert-border: #fde68a; --alert-fg: #92400e;
      --lane-claude-code: #7c3aed; --lane-codex: #2563eb; --lane-hermes: #d97706;
      --lane-opencode: #059669; --lane-openclaw: #0891b2; --lane-cursor: #9333ea; --lane-other: #64748b;
      --shadow-sm: 0 1px 2px rgba(15, 22, 34, 0.05);
      --shadow-md: 0 1px 2px rgba(15, 22, 34, 0.04), 0 10px 26px rgba(15, 22, 34, 0.07);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --color-bg: #090c12; --color-surface: #11151d; --color-surface-muted: #161c27;
        --color-border: #222a38; --color-border-strong: #303a4c;
        --color-text: #e7eaf1; --color-text-secondary: #c2c9d4;
        --color-text-muted: #9aa4b3; --color-text-faint: #6b7482; --color-muted: #9aa4b3;
        --color-accent: #818cf8; --color-cost: #34d399; --color-warn: #fbbf24;
        --color-log: #a78bfa; --color-bridge: #22d3ee;
        --status-ok-bg: rgba(52, 211, 153, 0.14); --status-ok-fg: #6ee7b7;
        --status-warn-bg: rgba(251, 191, 36, 0.14); --status-warn-fg: #fcd34d;
        --status-muted-bg: #1b222e; --status-muted-fg: #9aa4b3;
        --status-error-bg: rgba(248, 113, 113, 0.15); --status-error-fg: #fca5a5;
        --badge-bg: rgba(129, 140, 248, 0.16); --badge-fg: #c7d2fe;
        --notice-bg: rgba(52, 211, 153, 0.12); --notice-border: rgba(52, 211, 153, 0.3); --notice-fg: #6ee7b7;
        --alert-bg: rgba(251, 191, 36, 0.13); --alert-border: rgba(251, 191, 36, 0.3); --alert-fg: #fcd34d;
        --lane-claude-code: #a78bfa; --lane-codex: #60a5fa; --lane-hermes: #fbbf24;
        --lane-opencode: #34d399; --lane-openclaw: #22d3ee; --lane-cursor: #c084fc; --lane-other: #94a3b8;
        --shadow-sm: 0 1px 0 rgba(0, 0, 0, 0.4);
        --shadow-md: 0 1px 0 rgba(0, 0, 0, 0.5), 0 14px 34px rgba(0, 0, 0, 0.45);
      }
    }
    /* engineering-console numerics: mono + tabular so digits align */
    .value, .work-intro-metric strong, .work-overview-stat strong, .usage-pulse-metric strong,
    .run-metric strong, .bridge-stat .value, .identity-cell .value, .control-hero-stats strong,
    .task-fact strong, .work-fact strong, .metric strong {
      font-family: var(--font-mono); font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
    }
    td { font-variant-numeric: tabular-nums; }
    /* dark-mode fixes for components with hard-coded light backgrounds */
    @media (prefers-color-scheme: dark) {
      .sync-health { background: var(--color-surface-muted); }
      .model-chip { background: var(--color-surface-muted); border-color: var(--color-border); color: var(--color-text-secondary); }
      .task-brief { background: var(--color-surface-muted); border-color: var(--color-border-strong); }
      .task-brief-callout, .task-lane { background: var(--color-surface); }
      /* Descendant selectors so these win over the later light-gradient base
         rules regardless of source order (the base rules appear below this
         media block). */
      .agent-board .agent-board-copy, .usage-pulse .usage-pulse-copy { background: var(--color-surface-muted); }
      .task-work-preview { background: var(--color-surface-muted); }
      .task-work-preview-head { background: rgba(255, 255, 255, 0.03); }
      .control-boundary { background: rgba(34, 211, 238, 0.1); border-color: rgba(34, 211, 238, 0.32); color: #a5f3fc; }
      .control-boundary span { color: var(--color-text-muted); }
      .work-feed-item.finding-open { background: rgba(251, 191, 36, 0.07); border-color: rgba(251, 191, 36, 0.3); }
      .work-feed-item.action-required { background: rgba(248, 113, 113, 0.07); border-color: rgba(248, 113, 113, 0.3); }
      .work-feed-action { background: rgba(251, 146, 60, 0.09); border-color: rgba(251, 146, 60, 0.28); }
      .work-feed-finding { background: rgba(251, 191, 36, 0.09); border-color: rgba(251, 191, 36, 0.28); }
    }
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; color: var(--color-text); background: var(--color-bg); }
    main { max-width: 1280px; margin: 0 auto; padding: var(--space-7); }
    h1, h2 { margin: 0; }
    h1 { font-size: var(--font-h1); }
    h2 { font-size: var(--font-h2); }
    p { margin: 0; }
    .hero { align-items: flex-start; display: flex; gap: var(--space-6); justify-content: space-between; margin-bottom: 1.4rem; }
    .app-bar { align-items: center; display: flex; gap: var(--space-6); justify-content: space-between; margin-bottom: 2rem; }
    .brand { align-items: center; color: var(--color-text); display: inline-flex; font-size: 1.05rem; font-weight: 850; gap: var(--space-3); text-decoration: none; white-space: nowrap; }
    .brand-mark { background: var(--color-text); border-radius: 5px; color: white; display: inline-grid; font-size: var(--font-xs); height: 28px; place-items: center; width: 28px; }
    .app-bar .tabs { margin: 0; }
    .app-bar .actions { margin-left: auto; }
    .page-heading { margin: 0 0 1.35rem; }
    .page-heading .subtitle { max-width: 680px; }
    .eyebrow { color: var(--color-accent); font-size: var(--font-xs); font-weight: 700; letter-spacing: 0; text-transform: uppercase; }
    .subtitle { color: var(--color-text-muted); margin-top: var(--space-2); max-width: 740px; }
    .actions { align-items: center; display: flex; flex-wrap: wrap; gap: var(--space-3); justify-content: flex-end; }
    .button { background: var(--color-text); border: 0; border-radius: var(--radius-md); color: white; cursor: pointer; font-weight: 700; min-height: 40px; padding: 0 0.9rem; }
    .button.secondary { background: var(--color-surface); border: 1px solid var(--color-border-strong); color: var(--color-text); }
    .button-link { align-items: center; display: inline-flex; text-decoration: none; }
    .tabs { background: var(--color-text); border-radius: var(--radius-lg); display: inline-flex; flex-wrap: wrap; gap: var(--space-1); margin: 0 0 var(--space-5); padding: 0.25rem; }
    .tab-link { border-radius: var(--radius-sm); color: var(--color-border-strong); font-size: var(--font-nav); font-weight: 800; padding: 0.55rem 0.85rem; text-decoration: none; }
    .tab-link.active { background: var(--color-surface); color: var(--color-text); }
    .identity-strip { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); margin: 0 0 var(--space-6); }
    .identity-cell { border-right: 1px solid var(--color-border); padding: var(--space-5) var(--space-6); }
    .identity-cell:last-child { border-right: 0; }
    .identity-cell .value { font-size: var(--font-stat); }
    .nav { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); display: flex; flex-wrap: wrap; gap: var(--space-2); margin: 0 0 var(--space-6); padding: 0.45rem; }
    .nav a { color: var(--color-text-secondary); font-size: var(--font-body); font-weight: 700; padding: 0.45rem var(--space-4); text-decoration: none; }
    .notice { background: var(--notice-bg); border: 1px solid var(--notice-border); border-radius: var(--radius-lg); color: var(--notice-fg); font-weight: 700; margin: 0 0 var(--space-6); padding: var(--space-5) 0.9rem; }
    .sync-health { align-items: center; background: #f8fafc; border: 1px solid var(--color-border); border-radius: var(--radius-lg); display: flex; gap: var(--space-4); margin: 0 0 var(--space-6); padding: var(--space-4) var(--space-5); }
    .sync-health.degraded { background: #fff7ed; border-color: #fdba74; }
    .sync-health.healthy { background: #f0fdf4; border-color: #86efac; }
    .sync-health-dot { background: #94a3b8; border-radius: 999px; box-shadow: 0 0 0 4px rgba(148,163,184,0.16); flex: 0 0 auto; height: 0.58rem; width: 0.58rem; }
    .sync-health.degraded .sync-health-dot { background: #ea580c; box-shadow: 0 0 0 4px rgba(234,88,12,0.14); }
    .sync-health.healthy .sync-health-dot { background: #16a34a; box-shadow: 0 0 0 4px rgba(22,163,74,0.14); }
    .sync-health-copy { display: grid; flex: 1; gap: 0.15rem; min-width: 0; }
    .sync-health-copy strong { color: var(--color-text); }
    .sync-health-copy span { color: var(--color-muted); font-size: var(--font-sm); font-weight: 500; }
    .sync-health form { margin: 0; }
    .overview-freshness { align-items: baseline; color: var(--color-muted); display: flex; flex-wrap: wrap; font-size: var(--font-sm); gap: var(--space-2) var(--space-4); margin: calc(var(--space-3) * -1) 0 var(--space-4); }
    .overview-freshness strong { color: var(--color-text); }
    .overview-freshness span + span::before { color: var(--color-border-strong); content: "·"; margin-right: var(--space-4); }
    .metric-grid { display: grid; gap: 0.85rem; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); margin: var(--space-6) 0 1.4rem; }
    .section .metric-grid { padding: 0 var(--space-6); }
    .metric { background: var(--color-surface); border: 1px solid var(--color-border); border-top: 4px solid var(--color-accent); border-radius: var(--radius-lg); padding: 0.95rem; box-shadow: 0 1px 2px rgba(0,0,0,0.04); min-height: 112px; }
    .metric.cost { border-top-color: var(--color-cost); }
    .metric.warn { border-top-color: var(--color-warn); }
    .metric.log { border-top-color: var(--color-log); }
    .metric.bridge { border-top-color: var(--color-bridge); }
    .label { color: var(--color-text-muted); font-size: var(--font-sm); font-weight: 700; }
    .value { font-size: var(--font-value); font-weight: 800; margin-top: var(--space-1); }
    .section { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); margin-bottom: var(--space-6); overflow: hidden; }
    .section-header { align-items: baseline; display: flex; gap: var(--space-5); justify-content: space-between; padding: var(--space-6) var(--space-6) var(--space-4); }
    .section-note { color: var(--color-text-faint); font-size: var(--font-body); padding: 0 var(--space-6) 0.85rem; }
    .work-intro { align-items: flex-start; background: linear-gradient(135deg, #111827, #1f3a5f); border-radius: 12px; color: white; display: flex; gap: var(--space-6); justify-content: space-between; margin-bottom: var(--space-6); padding: 1.25rem 1.4rem; }
    .work-intro h2 { font-size: 1.45rem; }
    .work-intro p { color: #dbeafe; margin-top: var(--space-2); max-width: 760px; }
    .work-intro-metrics { display: flex; flex-wrap: wrap; gap: var(--space-3); justify-content: flex-end; }
    .work-intro-metric { background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.18); border-radius: var(--radius-lg); min-width: 104px; padding: var(--space-4) var(--space-5); }
    .work-intro-metric strong { display: block; font-size: var(--font-stat); }
    .work-intro-metric span { color: #dbeafe; font-size: var(--font-xs); }
    .work-overview { background: radial-gradient(circle at 90% 0, rgba(59,130,246,0.32), transparent 38%), linear-gradient(135deg, #0b1220, #172554 58%, #0f3b4d); border: 0; border-radius: 18px; box-shadow: 0 18px 46px rgba(15,23,42,0.16); color: #ffffff; display: grid; gap: 2rem; grid-template-columns: minmax(0, 1fr) auto; margin-bottom: var(--space-6); overflow: hidden; padding: 1.45rem 1.55rem; position: relative; }
    .work-overview::after { border: 1px solid rgba(255,255,255,0.12); border-radius: 50%; content: ""; height: 220px; position: absolute; right: -80px; top: -120px; width: 220px; }
    .work-overview-copy { position: relative; z-index: 1; }
    .work-overview-copy .eyebrow { color: #93c5fd; }
    .work-overview-copy h2 { font-size: 1.55rem; line-height: 1.15; margin-top: var(--space-2); }
    .work-overview-copy p { color: #cbd5e1; line-height: 1.5; margin-top: var(--space-3); max-width: 720px; }
    .work-overview-stats { align-items: center; display: flex; gap: var(--space-5); position: relative; z-index: 1; }
    .outcome-ring { align-items: center; background: conic-gradient(#4ade80 0 var(--verified-share), rgba(255,255,255,0.13) var(--verified-share) 100%); border-radius: 50%; display: grid; height: 106px; place-items: center; position: relative; width: 106px; }
    .outcome-ring::after { background: #111d34; border-radius: 50%; content: ""; inset: 9px; position: absolute; }
    .outcome-ring-label { position: relative; text-align: center; z-index: 1; }
    .outcome-ring-label strong { display: block; font-size: 1.45rem; line-height: 1; }
    .outcome-ring-label span { color: #bfdbfe; display: block; font-size: 0.68rem; margin-top: 0.25rem; text-transform: uppercase; }
    .work-overview-stat-stack { display: grid; gap: var(--space-2); min-width: 126px; }
    .work-overview-stat { align-items: center; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12); border-radius: var(--radius-md); display: flex; gap: var(--space-3); justify-content: space-between; min-width: 126px; padding: var(--space-3) var(--space-4); }
    .work-overview-stat strong { font-size: 1rem; }
    .work-overview-stat span { color: #cbd5e1; font-size: var(--font-xs); }
    .work-overview-stat.finding strong { color: #fcd34d; }
    .work-overview-stat.action strong { color: #fca5a5; }
    .usage-pulse { align-items: stretch; background: var(--color-surface); border: 1px solid var(--color-border); border-radius: 16px; box-shadow: 0 8px 24px rgba(15,23,42,0.05); display: grid; gap: 0; grid-template-columns: minmax(205px, 0.68fr) minmax(0, 1.7fr); margin-bottom: var(--space-6); overflow: hidden; }
    .usage-pulse-copy { background: linear-gradient(155deg, #ecfeff, #f8fafc); border-right: 1px solid var(--color-border); padding: 1.1rem 1.25rem; }
    .usage-pulse-copy h2 { font-size: 1.18rem; }
    .usage-pulse-copy p { color: var(--color-text-muted); font-size: var(--font-note); line-height: 1.45; margin-top: var(--space-3); }
    .usage-pulse-copy .see-all { display: inline-block; margin-top: var(--space-4); }
    .usage-pulse-main { display: flex; flex-direction: column; justify-content: center; min-width: 0; padding: 1rem 1.15rem; }
    .usage-pulse-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .usage-pulse-metric { border-right: 1px solid var(--color-border); min-width: 0; padding: 0 var(--space-4); }
    .usage-pulse-metric:first-child { padding-left: 0; }
    .usage-pulse-metric:last-child { border-right: 0; padding-right: 0; }
    .usage-pulse-metric strong { display: block; font-size: 0.94rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .usage-pulse-metric span { color: var(--color-text-faint); display: block; font-size: 0.66rem; margin-top: var(--space-1); text-transform: uppercase; }
    .usage-composition { background: var(--status-muted-bg); border-radius: var(--radius-pill); display: flex; height: 7px; margin-top: var(--space-5); overflow: hidden; }
    .usage-composition span { display: block; height: 100%; }
    .usage-composition .fresh, .usage-composition .uncached-input { background: #2563eb; }
    .usage-composition .output { background: #0891b2; }
    .usage-composition .cache-write { background: #8b5cf6; }
    .usage-composition .cache-read { background: #cbd5e1; }
    .usage-composition-note { color: var(--color-text-faint); font-size: 0.68rem; margin-top: var(--space-2); }
    .agent-board { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: 16px; box-shadow: 0 8px 24px rgba(15,23,42,0.06); display: grid; gap: 0; grid-template-columns: minmax(210px, 0.72fr) minmax(0, 1.8fr); margin-bottom: var(--space-6); overflow: hidden; }
    .agent-board-copy { background: linear-gradient(155deg, #eff6ff, #f8fafc); border-right: 1px solid var(--color-border); padding: 1.15rem 1.25rem; }
    .agent-board-copy h2 { font-size: 1.18rem; }
    .agent-board-copy p { color: var(--color-text-muted); font-size: var(--font-note); line-height: 1.45; margin-top: var(--space-3); }
    .agent-board-copy strong { color: var(--color-text); }
    .agent-roster { display: grid; gap: 0; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
    .agent-roster-item { border-right: 1px solid var(--color-border); min-width: 0; padding: 1rem; }
    .agent-roster-item:last-child { border-right: 0; }
    .agent-roster-head { align-items: center; display: flex; gap: var(--space-4); }
    .agent-avatar { align-items: center; border-radius: 10px; color: #ffffff; display: inline-flex; flex: 0 0 auto; font-size: 0.72rem; font-weight: 900; height: 38px; justify-content: center; letter-spacing: 0.02em; width: 38px; }
    .agent-roster-name { min-width: 0; }
    .agent-roster-name strong { display: block; font-size: var(--font-body); }
    .agent-roster-name span { color: var(--color-text-faint); display: block; font-size: var(--font-xs); margin-top: var(--space-1); }
    .agent-models { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-4); }
    .model-chip { background: #eef2ff; border: 1px solid #dbe4ff; border-radius: var(--radius-pill); color: #334155; display: inline-flex; font-size: 0.72rem; font-weight: 750; max-width: 100%; overflow: hidden; padding: 0.2rem 0.45rem; text-overflow: ellipsis; white-space: nowrap; }
    .model-chip.is-unknown { background: var(--color-surface-muted); border-color: var(--color-border); color: var(--color-text-faint); }
    .agent-volume { background: var(--status-muted-bg); border-radius: var(--radius-pill); height: 5px; margin-top: var(--space-4); overflow: hidden; }
    .agent-volume > span { display: block; height: 100%; min-width: 6px; }
    .agent-board-empty { color: var(--color-text-muted); padding: 1.25rem; }
    .run-section { background: transparent; border: 0; overflow: visible; }
    .run-section > .section-header { padding: 0 0 var(--space-5); }
    .run-section > .section-header p { color: var(--color-text-faint); font-size: var(--font-note); margin-top: var(--space-2); }
    .work-feed { display: grid; gap: 1rem; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .work-feed-item { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: 14px; box-shadow: 0 7px 20px rgba(15,23,42,0.06); min-width: 0; overflow: hidden; padding: 0; transition: border-color 120ms ease, box-shadow 120ms ease; }
    .work-feed-item.finding-open { background: #fffdf5; border-color: #fde68a; }
    .work-feed-item.action-required { background: #fffafa; border-color: #fecaca; }
    .run-card-shell { min-height: 100%; padding: 1rem 1rem 0; position: relative; }
    .run-card-accent { border-radius: 0 0 var(--radius-pill) var(--radius-pill); height: 48px; left: 0; position: absolute; top: 0; width: 5px; }
    .run-card-header { align-items: flex-start; display: flex; gap: var(--space-4); justify-content: space-between; }
    .run-identity { align-items: center; display: flex; gap: var(--space-4); min-width: 0; }
    .run-identity-copy { min-width: 0; }
    .run-agent-line { align-items: center; display: flex; flex-wrap: wrap; gap: var(--space-2); }
    .run-agent-line strong { font-size: var(--font-body); }
    .run-session-line { color: var(--color-text-faint); font-size: var(--font-xs); margin-top: var(--space-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .work-feed-title { font-size: 1.02rem; line-height: 1.35; margin: 0.95rem 0 0; }
    .work-feed-meta { color: var(--color-text-faint); font-size: var(--font-xs); margin-top: var(--space-2); }
    .run-flow { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 1rem 0 0; }
    .run-step { color: var(--color-text-faint); font-size: 0.68rem; padding-top: 1rem; position: relative; text-align: center; }
    .run-step::before { background: var(--color-border-strong); border: 3px solid var(--color-surface); border-radius: 50%; box-shadow: 0 0 0 1px var(--color-border-strong); content: ""; height: 9px; left: 50%; position: absolute; top: 0; transform: translate(-50%, -50%); width: 9px; z-index: 1; }
    .run-step::after { background: var(--color-border); content: ""; height: 2px; left: -50%; position: absolute; right: 50%; top: 0; }
    .run-step:first-child::after { display: none; }
    .run-step.is-done { color: var(--status-ok-fg); font-weight: 750; }
    .run-step.is-done::before { background: #22c55e; box-shadow: 0 0 0 1px #22c55e; }
    .run-step.is-reported { color: #475569; font-weight: 750; }
    .run-step.is-reported::before { background: #64748b; box-shadow: 0 0 0 1px #64748b; }
    .run-step.is-active { color: var(--status-warn-fg); font-weight: 750; }
    .run-step.is-active::before { animation: run-pulse 1.8s ease-in-out infinite; background: #f59e0b; box-shadow: 0 0 0 1px #f59e0b; }
    .run-step.is-warning { color: var(--status-error-fg); font-weight: 750; }
    .run-step.is-warning::before { background: #ef4444; box-shadow: 0 0 0 1px #ef4444; }
    .run-step.is-finding { color: var(--status-warn-fg); font-weight: 750; }
    .run-step.is-finding::before { background: #f59e0b; box-shadow: 0 0 0 1px #f59e0b; }
    @keyframes run-pulse { 0%, 100% { box-shadow: 0 0 0 1px #f59e0b, 0 0 0 0 rgba(245,158,11,0.35); } 50% { box-shadow: 0 0 0 1px #f59e0b, 0 0 0 5px rgba(245,158,11,0); } }
    .run-metrics { border-top: 1px solid var(--color-border); display: grid; gap: 0; grid-template-columns: repeat(auto-fit, minmax(92px, 1fr)); margin: 1rem -1rem 0; }
    .run-metric { border-right: 1px solid var(--color-border); min-width: 0; padding: 0.65rem 0.75rem; }
    .run-metric:last-child { border-right: 0; }
    .run-metric strong { display: block; font-size: 0.82rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .run-metric span { color: var(--color-text-faint); display: block; font-size: 0.66rem; margin-top: var(--space-1); text-transform: uppercase; }
    .work-feed-action { background: #fff7ed; border: 1px solid #fed7aa; border-radius: var(--radius-md); color: var(--color-text-secondary); margin-top: var(--space-5); padding: var(--space-4) var(--space-5); }
    .work-feed-action strong { color: #9a3412; font-size: var(--font-xs); text-transform: uppercase; }
    .work-feed-action p + strong { display: block; margin-top: var(--space-3); }
    .work-feed-action p { line-height: 1.45; margin-top: var(--space-1); }
    .work-feed-finding { background: #fffbeb; border: 1px solid #fde68a; border-radius: var(--radius-md); color: var(--color-text-secondary); margin-top: var(--space-5); padding: var(--space-4) var(--space-5); }
    .work-feed-finding-head { align-items: center; display: flex; flex-wrap: wrap; gap: var(--space-2); justify-content: space-between; }
    .work-feed-finding-head strong { color: var(--status-warn-fg); font-size: var(--font-xs); text-transform: uppercase; }
    .work-feed-finding-head span { color: var(--color-text-faint); font-size: var(--font-xs); }
    .work-feed-finding > p { line-height: 1.45; margin-top: var(--space-2); }
    .work-feed-finding-context { color: var(--color-text-muted); font-size: var(--font-note); }
    .task-work-preview { background: linear-gradient(145deg, #f8fafc, #f1f5f9); border: 1px solid var(--color-border); border-radius: 10px; display: grid; gap: 0; margin-top: var(--space-5); overflow: hidden; }
    .task-work-preview-head { align-items: center; background: rgba(255,255,255,0.66); border-bottom: 1px solid var(--color-border); display: flex; justify-content: space-between; padding: 0.58rem 0.68rem; }
    .task-work-preview-head strong { color: var(--color-text-secondary); font-size: var(--font-xs); text-transform: uppercase; }
    .task-work-preview-head span { color: var(--color-text-faint); font-size: var(--font-xs); }
    .task-work-list { list-style: none; margin: 0; padding: 0; }
    .task-work-preview-row { align-items: center; border-top: 1px solid var(--color-border); display: grid; gap: var(--space-3); grid-template-columns: 26px minmax(0, 1fr) auto; min-width: 0; padding: 0.55rem 0.68rem; }
    .task-work-preview-row:first-child { border-top: 0; }
    .task-work-index { align-items: center; border-radius: 50%; display: inline-flex; font-size: 0.66rem; font-weight: 800; height: 24px; justify-content: center; padding: 0; width: 24px; }
    .task-work-title { font-size: var(--font-note); font-weight: 700; line-height: 1.35; min-width: 0; overflow-wrap: anywhere; white-space: normal; }
    .task-work-preview-row small { color: var(--color-text-faint); font-size: 0.68rem; white-space: nowrap; }
    .task-work-expander { border-top: 1px solid var(--color-border); }
    .task-work-expander summary { align-items: center; color: var(--color-accent); cursor: pointer; display: flex; font-size: var(--font-note); font-weight: 750; justify-content: space-between; list-style: none; min-height: 44px; padding: 0 var(--space-4); }
    .task-work-expander summary::-webkit-details-marker { display: none; }
    .task-work-expander summary::after { content: "+"; font-size: 1rem; }
    .task-work-expander[open] summary::after { content: "−"; }
    .task-work-expander summary:focus-visible { box-shadow: inset 0 0 0 2px var(--color-accent); outline: 0; }
    .task-work-expander-open { display: none; }
    .task-work-expander[open] .task-work-expander-closed { display: none; }
    .task-work-expander[open] .task-work-expander-open { display: inline; }
    .task-work-overflow { border-top: 1px solid var(--color-border); max-height: 22rem; overflow-y: auto; overscroll-behavior: contain; }
    .task-title-link { color: inherit; text-decoration-color: rgba(37,99,235,0.32); text-decoration-thickness: 2px; text-underline-offset: 0.18em; }
    .task-title-link:hover { color: var(--color-accent); text-decoration-color: currentColor; }
    .activation-card { background: linear-gradient(135deg, #0f172a 0%, #172554 58%, #1e3a8a 100%); border: 0; border-radius: 20px; color: white; display: grid; gap: var(--space-6); margin-bottom: var(--space-7); overflow: hidden; padding: clamp(1.35rem, 3vw, 2.25rem); position: relative; }
    .activation-card::after { background: radial-gradient(circle, rgba(96,165,250,0.28), transparent 68%); content: ""; height: 18rem; pointer-events: none; position: absolute; right: -7rem; top: -8rem; width: 18rem; }
    .activation-head { display: grid; gap: var(--space-3); max-width: 52rem; position: relative; z-index: 1; }
    .activation-head .eyebrow { color: #bfdbfe; }
    .activation-head h2 { color: white; font-size: clamp(1.55rem, 4vw, 2.25rem); line-height: 1.08; margin: 0; }
    .activation-head p { color: #dbeafe; font-size: 0.98rem; margin: 0; max-width: 46rem; }
    .activation-steps { display: grid; gap: var(--space-2); grid-template-columns: repeat(5, minmax(0, 1fr)); position: relative; z-index: 1; }
    .activation-step { background: rgba(255,255,255,0.08); border: 1px solid rgba(191,219,254,0.2); border-radius: 12px; min-height: 5.1rem; padding: var(--space-3); }
    .activation-step strong { display: block; font-size: 0.78rem; line-height: 1.25; }
    .activation-step span { color: #bfdbfe; display: block; font-size: 0.7rem; margin-top: var(--space-2); text-transform: capitalize; }
    .activation-step.is-ready, .activation-step.is-captured { background: rgba(16,185,129,0.16); border-color: rgba(110,231,183,0.45); }
    .activation-step.is-needs-action, .activation-step.is-restart-required { background: rgba(245,158,11,0.14); border-color: rgba(253,230,138,0.45); }
    .activation-action { align-items: center; background: white; border-radius: 14px; color: #0f172a; display: flex; flex-wrap: wrap; gap: var(--space-4); justify-content: space-between; padding: var(--space-4); position: relative; z-index: 1; }
    .activation-action-copy strong, .activation-action-copy span { display: block; }
    .activation-action-copy span { color: #475569; font-size: 0.78rem; margin-top: 0.2rem; }
    .activation-command { background: #e2e8f0; border-radius: 8px; color: #0f172a; font-size: 0.76rem; max-width: 100%; overflow-wrap: anywhere; padding: 0.65rem 0.8rem; }
    .activation-permission { color: #bfdbfe; font-size: 0.72rem; margin: 0; position: relative; z-index: 1; }
    .skip-link { background: var(--color-text); border-radius: 0 0 8px 8px; color: white; left: 1rem; padding: 0.7rem 1rem; position: fixed; top: -5rem; z-index: 100; }
    .skip-link:focus { top: 0; }
    :focus-visible { outline: 3px solid rgba(37,99,235,0.55); outline-offset: 3px; }
    .task-detail-back { display: inline-flex; font-weight: 750; margin-bottom: var(--space-5); min-height: 44px; align-items: center; }
    .task-brief { background: linear-gradient(145deg,#eff6ff,#fff); border: 1px solid #bfdbfe; border-radius: var(--radius-lg); box-shadow: var(--shadow-sm); display: grid; gap: var(--space-5); padding: clamp(1rem,2vw,1.6rem); }
    .task-brief h2 { font-size: clamp(1.25rem,2vw,1.7rem); line-height: 1.25; }
    .task-brief-proof { display: grid; gap: var(--space-3); grid-template-columns: repeat(2,minmax(0,1fr)); }
    .task-brief-callout { background: rgba(255,255,255,0.75); border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: var(--space-4); }
    .task-brief-callout strong { display: block; font-size: var(--font-xs); margin-bottom: var(--space-2); text-transform: uppercase; }
    .task-state-axis { display: grid; gap: var(--space-3); grid-template-columns: repeat(3,minmax(0,1fr)); }
    .task-state-cell { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: var(--space-4); }
    .task-state-cell small { color: var(--color-text-faint); display: block; font-weight: 800; margin-bottom: var(--space-2); text-transform: uppercase; }
    .task-state-cell strong { font-size: 1rem; }
    .task-facts { display: grid; gap: var(--space-3); grid-template-columns: repeat(auto-fit,minmax(145px,1fr)); }
    .task-fact { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: var(--space-4); }
    .task-fact span { color: var(--color-text-faint); display: block; font-size: var(--font-xs); font-weight: 750; margin-bottom: var(--space-2); }
    .task-lane-strip { display: flex; flex-wrap: wrap; gap: var(--space-2); }
    .task-lane { background: rgba(255,255,255,0.78); border: 1px solid var(--color-border); border-radius: var(--radius-pill); display: inline-flex; gap: var(--space-2); padding: 0.45rem 0.7rem; }
    .task-lane strong { font-size: 0.75rem; }
    .task-lane span { color: var(--color-text-faint); font-size: 0.7rem; }
    .coverage-grid { display: grid; gap: var(--space-3); grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); }
    .coverage-card { border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: var(--space-4); }
    .coverage-card strong { display: block; margin-bottom: var(--space-2); text-transform: capitalize; }
    .coverage-card small { color: var(--color-text-faint); display: block; margin-top: var(--space-2); }
    .task-timeline { list-style: none; margin: 0; padding: 0; }
    .task-timeline-row { border-left: 2px solid #cbd5e1; display: grid; gap: var(--space-2); grid-template-columns: minmax(120px,0.35fr) minmax(0,1fr) auto; margin-left: 0.7rem; padding: 0 0 var(--space-5) 1.25rem; position: relative; }
    .task-timeline-row::before { background: var(--color-surface); border: 3px solid var(--color-accent); border-radius: 50%; content: ""; height: 12px; left: -7px; position: absolute; top: 0.25rem; width: 12px; }
    .task-timeline-meta { color: var(--color-text-faint); font-size: var(--font-note); }
    .task-timeline-title { font-weight: 750; overflow-wrap: anywhere; }
    .task-lane-chip { background: var(--color-surface-muted); border-radius: 999px; color: var(--color-text-secondary); font-size: var(--font-xs); font-weight: 800; padding: 0.25rem 0.55rem; white-space: nowrap; }
    .task-detail-section { margin-top: var(--space-7); }
    .task-detail-section > h2 { margin-bottom: var(--space-4); }
    .work-feed-why { border-top: 1px solid var(--color-border); margin: 0 -1rem; }
    .work-feed-why summary { color: var(--color-accent); cursor: pointer; font-size: var(--font-note); font-weight: 700; list-style: none; padding: var(--space-4) 1rem; }
    .work-feed-why summary::-webkit-details-marker { display: none; }
    .work-feed-why[open] { background: var(--color-surface-muted); }
    .work-feed-why p { color: var(--color-text-muted); font-size: var(--font-note); line-height: 1.5; padding: 0 1rem var(--space-4); }
    .work-feed-why p + p { padding-top: 0; }
    .run-detail-block { padding: 0 1rem var(--space-4); }
    .run-detail-block > strong { color: var(--color-text-secondary); display: block; font-size: var(--font-xs); margin-bottom: var(--space-2); text-transform: uppercase; }
    .run-detail-list { display: grid; gap: var(--space-2); list-style: none; margin: 0; padding: 0; }
    .run-detail-list li { align-items: center; display: flex; gap: var(--space-2); min-width: 0; }
    .run-detail-list li > span:last-child { color: var(--color-text-muted); font-size: var(--font-note); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .task-detail-list small { color: var(--color-text-faint); display: block; font-size: 0.68rem; margin-top: var(--space-1); }
    .task-controls { border-top: 1px dashed var(--color-border-strong); padding-top: var(--space-4); }
    .task-controls > .note { padding: 0 0 var(--space-3); }
    .task-control-row { align-items: center; border-top: 1px solid var(--color-border); display: flex; gap: var(--space-4); justify-content: space-between; padding: var(--space-3) 0; }
    .task-control-row span { min-width: 0; }
    .task-control-row strong, .task-control-row small { display: block; }
    .task-control-row strong { color: var(--color-text-secondary); font-size: var(--font-note); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .task-control-row small { color: var(--color-text-faint); font-size: var(--font-xs); margin-top: var(--space-1); }
    .task-control-button { background: var(--color-surface); border: 1px solid var(--color-border-strong); border-radius: var(--radius-sm); color: var(--color-text-secondary); cursor: pointer; flex: 0 0 auto; font-size: var(--font-xs); font-weight: 750; min-height: 32px; padding: 0 var(--space-4); }
    .task-control-button:hover { border-color: var(--color-accent); color: var(--color-accent); }
    .task-control-button.is-danger { color: var(--status-error-fg); }
    .finding-review-list { display: grid; gap: var(--space-3); }
    .finding-review-row { background: var(--color-surface-muted); border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: var(--space-3); }
    .finding-review-head { align-items: center; display: flex; gap: var(--space-2); justify-content: space-between; }
    .finding-review-head strong { color: var(--color-text); font-size: var(--font-note); text-transform: none; }
    .finding-review-row > p { color: var(--color-text-secondary); font-size: var(--font-note); margin: var(--space-2) 0; }
    .finding-review-actions { align-items: end; display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-3); }
    .finding-review-actions form { align-items: end; display: flex; flex-wrap: wrap; gap: var(--space-2); }
    .finding-review-note { min-width: min(320px, 70vw); }
    .finding-review-note input { background: var(--color-surface); border: 1px solid var(--color-border-strong); border-radius: var(--radius-sm); color: var(--color-text); min-height: 32px; padding: 0 var(--space-3); width: 100%; }
    .task-rename-form { display: grid; gap: var(--space-3); grid-template-columns: minmax(0, 1fr) auto; padding-bottom: var(--space-3); }
    .task-rename-form input { background: var(--color-surface); border: 1px solid var(--color-border-strong); border-radius: var(--radius-sm); color: var(--color-text); min-height: 36px; padding: 0 var(--space-4); width: 100%; }
    .supporting-review-rollup { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); grid-column: 1 / -1; overflow: hidden; }
    .supporting-review-rollup > summary { align-items: center; cursor: pointer; display: flex; gap: var(--space-4); justify-content: space-between; list-style: none; padding: 0.9rem 1rem; }
    .supporting-review-rollup > summary::-webkit-details-marker { display: none; }
    .supporting-review-rollup > summary strong { display: block; font-size: 0.88rem; }
    .supporting-review-rollup > summary span span { color: var(--color-text-muted); display: block; font-size: var(--font-note); margin-top: var(--space-1); }
    .supporting-review-count { background: var(--status-muted-bg); border-radius: var(--radius-pill); color: var(--color-text-secondary); font-size: var(--font-xs); font-weight: 800; padding: 0.25rem 0.55rem; }
    .supporting-review-list { border-top: 1px solid var(--color-border); display: grid; gap: 0; list-style: none; margin: 0; padding: 0; }
    .supporting-review-list li { align-items: center; border-bottom: 1px solid var(--color-border); display: grid; gap: var(--space-3); grid-template-columns: auto minmax(0, 1fr); padding: 0.7rem 1rem; }
    .supporting-review-list li:last-child { border-bottom: 0; }
    .supporting-review-list strong { display: block; font-size: var(--font-note); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .supporting-review-list small { color: var(--color-text-faint); display: block; font-size: var(--font-xs); margin-top: var(--space-1); }
    .work-feed-footer { align-items: center; display: flex; gap: var(--space-5); justify-content: space-between; padding: var(--space-5) 0 0; }
    .work-feed-footer p { color: var(--color-text-faint); font-size: var(--font-note); }
    .attention-list, .work-card-list, .source-coverage-grid, .advanced-grid { display: grid; gap: var(--space-5); padding: 0 var(--space-6) var(--space-6); }
    .attention-list { grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
    .attention-item { border: 1px solid var(--color-border); border-left: 4px solid var(--color-warn); border-radius: var(--radius-lg); padding: 0.9rem; }
    .attention-item.high { border-left-color: var(--status-error-fg); }
    .attention-item.info, .attention-item.low { border-left-color: var(--color-bridge); }
    .attention-item h3, .work-card h3, .source-coverage-card h3, .advanced-card h3 { font-size: 1rem; margin: var(--space-3) 0 var(--space-2); }
    .attention-item p { color: var(--color-text-secondary); font-size: var(--font-body); line-height: 1.45; }
    .attention-next { background: var(--color-surface-muted); border-radius: var(--radius-sm); margin-top: var(--space-4); padding: var(--space-4); }
    .attention-next strong { color: var(--color-text-secondary); display: block; font-size: var(--font-2xs); margin-bottom: var(--space-1); text-transform: uppercase; }
    .work-card-list { grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
    .work-card { border: 1px solid var(--color-border); border-radius: 10px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); overflow: hidden; }
    .work-card-header { align-items: flex-start; display: flex; gap: var(--space-4); justify-content: space-between; padding: 0.9rem 1rem var(--space-4); }
    .work-card-header h3 { margin: 0; }
    .work-card-meta { color: var(--color-text-faint); font-size: var(--font-note); margin-top: var(--space-2); }
    .work-card-summary { color: var(--color-text-secondary); line-height: 1.5; padding: 0 1rem var(--space-5); }
    .work-card-facts { background: var(--color-surface-muted); border-top: 1px solid var(--color-border); display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .work-fact { border-right: 1px solid var(--color-border); min-width: 0; padding: var(--space-4) var(--space-5); }
    .work-fact:last-child { border-right: 0; }
    .work-fact strong { display: block; font-size: var(--font-body); overflow-wrap: anywhere; }
    .work-fact span { color: var(--color-text-faint); font-size: var(--font-2xs); }
    .work-card-next { border-top: 1px dashed var(--color-border); color: var(--color-text-secondary); font-size: var(--font-note); padding: var(--space-4) 1rem; }
    .evidence-drawer { border-top: 1px solid var(--color-border); }
    .evidence-drawer summary { color: var(--color-accent); cursor: pointer; font-size: var(--font-body); font-weight: 700; list-style: none; padding: var(--space-4) 1rem; }
    .evidence-drawer summary::-webkit-details-marker { display: none; }
    .evidence-drawer[open] { background: var(--color-surface-muted); }
    .evidence-facts { display: grid; gap: var(--space-3); grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); padding: 0 1rem 1rem; }
    .evidence-fact { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-sm); font-size: var(--font-note); padding: var(--space-4); }
    .source-coverage-grid { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .source-coverage-card { border: 1px solid var(--color-border); border-radius: var(--radius-lg); padding: 0.85rem; }
    .source-coverage-card .work-card-header { padding: 0; }
    .source-coverage-card h3 { margin: 0; }
    .source-coverage-meta { color: var(--color-text-faint); font-size: var(--font-note); line-height: 1.5; margin-top: var(--space-3); }
    .source-coverage-stats { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-4); }
    .advanced-grid { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .advanced-card { border: 1px solid var(--color-border); border-radius: var(--radius-lg); display: flex; flex-direction: column; min-height: 170px; padding: 1rem; }
    .advanced-card h3 { margin: 0 0 var(--space-3); }
    .advanced-card p { color: var(--color-text-muted); flex: 1; font-size: var(--font-body); line-height: 1.5; }
    .advanced-card a { color: var(--color-accent); font-weight: 700; margin-top: var(--space-5); text-decoration: none; }
    .see-all { color: var(--color-accent); font-size: var(--font-body); font-weight: 700; text-decoration: none; white-space: nowrap; }
    .split-grid { display: grid; gap: var(--space-6); grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); padding: 0 var(--space-6) var(--space-6); }
    .inline-panel { border: 1px solid var(--color-border); border-radius: var(--radius-lg); overflow: hidden; }
    .inline-title { background: var(--color-surface-muted); color: var(--color-text-secondary); font-size: var(--font-2xs); font-weight: 800; padding: var(--space-4) var(--space-5); text-transform: uppercase; }
    .bridge-overview { border-top: 1px solid var(--color-border); display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
    .bridge-stat { border-right: 1px solid var(--color-border); padding: 0.85rem var(--space-6); }
    .bridge-stat:last-child { border-right: 0; }
    .bridge-stat .value { font-size: var(--font-stat); }
    .bridge-alert { background: var(--alert-bg); border-top: 1px solid var(--alert-border); color: var(--alert-fg); font-weight: 700; padding: var(--space-5) var(--space-6); }
    .subsection-title { background: var(--color-surface-muted); border-top: 1px solid var(--color-border); color: var(--color-text-secondary); font-size: var(--font-2xs); font-weight: 800; padding: var(--space-4) var(--space-5); text-transform: uppercase; }
    .subsection-title.with-controls { align-items: center; display: flex; flex-wrap: wrap; gap: var(--space-5); justify-content: space-between; }
    .subsection-title .sort-controls { font-weight: 400; text-transform: none; }
    .sort-controls { color: var(--color-text-faint); display: flex; flex-wrap: wrap; gap: 0.4rem; justify-content: flex-end; }
    .sort-link { border: 1px solid var(--color-border-strong); border-radius: var(--radius-pill); color: var(--color-text-secondary); font-size: var(--font-sm); padding: var(--space-1) 0.55rem; text-decoration: none; }
    .sort-link.active { background: var(--color-text); border-color: var(--color-text); color: white; }
    .workflow { display: grid; gap: var(--space-5); grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); padding: 0 var(--space-6) var(--space-6); }
    .step { border: 1px solid var(--color-border); border-radius: var(--radius-lg); padding: 0.8rem; }
    .step strong { display: block; margin-bottom: var(--space-2); }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; background: var(--color-surface); }
    th, td { border-top: 1px solid var(--color-border); padding: var(--space-4) var(--space-5); text-align: left; vertical-align: top; }
    th { background: var(--color-surface-muted); color: var(--color-text-secondary); font-size: var(--font-2xs); text-transform: uppercase; }
    .status { border-radius: var(--radius-pill); display: inline-block; font-size: var(--font-chip); padding: 0.15rem 0.55rem; }
    .status-found { background: var(--status-ok-bg); color: var(--status-ok-fg); }
    .status-ready { background: var(--status-ok-bg); color: var(--status-ok-fg); }
    .status-needs-import { background: var(--status-warn-bg); color: var(--status-warn-fg); }
    .status-finding { background: var(--status-warn-bg); color: var(--status-warn-fg); }
    .status-missing { background: var(--status-muted-bg); color: var(--status-muted-fg); }
    .status-error { background: var(--status-error-bg); color: var(--status-error-fg); }
    .capability-table { min-width: 1840px; }
    .capability-table td { min-width: 180px; }
    .capability-table td:first-child { min-width: 220px; }
    .capability-table small, .capability-table .note { display: block; line-height: 1.35; margin-top: var(--space-2); }
    .capability-table small { color: var(--color-text-secondary); font-size: var(--font-xs); }
    .capability-detail { margin-top: var(--space-2); }
    .capability-detail summary, .capability-roadmap > summary { color: var(--color-accent); cursor: pointer; font-size: var(--font-xs); font-weight: 700; }
    .capability-detail li { color: var(--color-text-faint); font-size: var(--font-xs); line-height: 1.35; }
    .capability-roadmap { border-top: 1px solid var(--color-border); padding: var(--space-5) 0 0; }
    .capability-roadmap > summary { margin: 0 var(--space-5) var(--space-5); }
    code { white-space: pre-wrap; word-break: break-word; }
    .note { color: var(--color-text-faint); font-size: var(--font-note); }
    ul { margin: var(--space-2) 0 0; padding-left: 1.1rem; }
    .session-row { border-top: 1px solid var(--color-border); }
    .session-row summary { cursor: pointer; display: block; list-style: none; padding: 0.6rem var(--space-6); }
    .session-row summary::-webkit-details-marker { display: none; }
    .session-row[open] { background: var(--color-surface-muted); }
    .session-line { line-height: 1.6; }
    .session-goal { font-weight: 700; }
    .session-meta { color: var(--color-text-faint); font-size: var(--font-note); margin-top: var(--space-1); }
    .session-children { margin-top: var(--space-1); }
    .session-detail { border-top: 1px dashed var(--color-border); padding: var(--space-4) var(--space-6) 0.9rem; }
    .detail-title { color: var(--color-text-secondary); font-size: var(--font-2xs); font-weight: 800; margin-top: var(--space-3); text-transform: uppercase; }
    .detail-title:first-child { margin-top: 0; }
    .badge-client { background: var(--badge-bg); border-radius: var(--radius-pill); color: var(--badge-fg); display: inline-block; font-size: var(--font-xs); font-weight: 700; padding: 0.15rem 0.55rem; }
    .chip { background: var(--status-muted-bg); border-radius: var(--radius-pill); color: var(--status-muted-fg); display: inline-block; font-size: var(--font-2xs); padding: 0.1rem 0.5rem; }
    .empty-state { color: var(--color-text-faint); padding: 0.9rem var(--space-6) 1.1rem; }
    .footer { align-items: baseline; color: var(--color-text-faint); display: flex; flex-wrap: wrap; gap: var(--space-3) var(--space-6); justify-content: space-between; padding: var(--space-6) 0 var(--space-3); }
    .footer-links { display: inline-flex; gap: var(--space-4); }
    .footer-links a { color: var(--color-text-faint); font-size: var(--font-2xs); text-decoration: none; }
    .footer-links a:hover { color: var(--color-accent); text-decoration: underline; }
    #tokens-explorer tbody tr:hover, #usage-basics tbody tr:hover { background: var(--color-surface-muted); }
    .lane-claude-code { background: var(--lane-claude-code); }
    .lane-codex { background: var(--lane-codex); }
    .lane-hermes { background: var(--lane-hermes); }
    .lane-opencode { background: var(--lane-opencode); }
    .lane-openclaw { background: var(--lane-openclaw); }
    .lane-cursor { background: var(--lane-cursor); }
    .lane-other { background: var(--lane-other); }
    .badge-client.lane-claude-code, .badge-client.lane-codex, .badge-client.lane-hermes, .badge-client.lane-opencode, .badge-client.lane-openclaw, .badge-client.lane-cursor, .badge-client.lane-other { color: #ffffff; }
    .filter-rows { display: grid; gap: var(--space-3); padding: 0 var(--space-6) var(--space-4); }
    .filter-row { align-items: center; display: flex; flex-wrap: wrap; gap: 0.4rem; }
    .filter-label { color: var(--color-text-muted); font-size: var(--font-sm); font-weight: 700; min-width: 92px; }
    .bar { background: var(--status-muted-bg); border-radius: 3px; height: 8px; margin-top: var(--space-2); max-width: 220px; overflow: hidden; }
    .bar-fill { display: block; height: 100%; }
    .chart { margin: 0; padding: 0 var(--space-6) var(--space-4); }
    .chart svg { display: block; height: auto; width: 100%; }
    .chart-grid { stroke: var(--color-border); stroke-width: 1; }
    .chart-axis-label { fill: var(--color-text-faint); font-size: 11px; }
    .chart-band-label { fill: var(--color-text-muted); font-size: 11px; font-weight: 700; }
    .chart-bar.lane-claude-code { fill: var(--lane-claude-code); }
    .chart-bar.lane-codex { fill: var(--lane-codex); }
    .chart-bar.lane-hermes { fill: var(--lane-hermes); }
    .chart-bar.lane-opencode { fill: var(--lane-opencode); }
    .chart-bar.lane-openclaw { fill: var(--lane-openclaw); }
    .chart-bar.lane-cursor { fill: var(--lane-cursor); }
    .chart-bar.lane-other { fill: var(--lane-other); }
    .chart-bar.chart-cache-write { opacity: 0.45; }
    .chart-hover { fill: transparent; }
    .chart-read { fill: var(--color-border-strong); }
    .chart-legend { color: var(--color-text-faint); font-size: var(--font-note); padding: var(--space-3) 0 0; }
    .legend-swatch { border-radius: 2px; display: inline-block; height: 10px; margin-right: 0.3rem; vertical-align: baseline; width: 10px; }
    .swatch-cache-write { background: var(--color-text-faint); opacity: 0.45; }
    .swatch-cache-read { background: var(--color-border-strong); }
    /* Overview v2: metric tiles, usage charts, breakdown tabs, roster usage,
       top sessions, expandable per-task sessions. All token-driven so the dark
       media query above re-themes them with zero extra rules. */
    .ov-metric-grid { margin-top: var(--space-3); }
    .ov-usage .section-header { padding-bottom: var(--space-3); }
    .ov-chart-block { padding: 0 0 var(--space-4); }
    .ov-chart-subhead { align-items: baseline; border-top: 1px solid var(--color-border); color: var(--color-text-faint); display: flex; flex-wrap: wrap; font-size: 0.7rem; font-weight: 700; gap: var(--space-3); justify-content: space-between; letter-spacing: 0.06em; padding: var(--space-4) var(--space-6) var(--space-2); text-transform: uppercase; }
    .ov-chart-subhead .note { font-weight: 500; letter-spacing: 0; text-transform: none; }
    .ov-utabs { background: var(--color-surface-muted); border: 1px solid var(--color-border); border-radius: 8px; display: inline-flex; gap: 2px; letter-spacing: 0; padding: 2px; text-transform: none; }
    .ov-utab { border-radius: 6px; color: var(--color-text-muted); font-size: 0.72rem; font-weight: 650; min-height: 28px; padding: 0.2rem 0.55rem; text-decoration: none; display: inline-flex; align-items: center; }
    .ov-utab.is-on { background: var(--color-surface); box-shadow: var(--shadow-sm); color: var(--color-text); }
    /* CSS-only breakdown tabs: visually-hidden radios drive which panel + label
       is active via :checked ~ sibling rules. Zero JS, zero page navigation. */
    .ov-usage-bars .ov-utab-radio { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; border: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
    .ov-utab { cursor: pointer; }
    .ov-usage-bars .ov-utab-panel { display: none; }
    .ov-usage-bars #ov-bd-agent:checked ~ .ov-utab-panel-agent,
    .ov-usage-bars #ov-bd-model:checked ~ .ov-utab-panel-model,
    .ov-usage-bars #ov-bd-agent-model:checked ~ .ov-utab-panel-agent-model { display: block; }
    .ov-usage-bars #ov-bd-agent:checked ~ .ov-chart-subhead label[for="ov-bd-agent"],
    .ov-usage-bars #ov-bd-model:checked ~ .ov-chart-subhead label[for="ov-bd-model"],
    .ov-usage-bars #ov-bd-agent-model:checked ~ .ov-chart-subhead label[for="ov-bd-agent-model"] { background: var(--color-surface); box-shadow: var(--shadow-sm); color: var(--color-text); }
    .ov-usage-bars .ov-utab-radio:focus-visible ~ .ov-chart-subhead .ov-utabs { outline: 2px solid var(--color-text); outline-offset: 2px; }
    .ov-linechart svg, .ov-barchart svg { max-height: 220px; }
    .ov-line-path { fill: none; stroke: var(--color-accent); stroke-width: 2; }
    .ov-line-dot { fill: var(--color-surface); stroke: var(--color-accent); stroke-width: 2; }
    .ov-line-g0 { stop-color: var(--color-accent); stop-opacity: 0.24; }
    .ov-line-g1 { stop-color: var(--color-accent); stop-opacity: 0; }
    /* No-JS instant hover tooltip: a full-height transparent hit zone per day
       reveals a styled crosshair + tooltip via CSS :hover (the CSP forbids
       script, so this is how the charts stay interactive). */
    .ovh-hit { fill: transparent; cursor: crosshair; }
    .ovh-cross { opacity: 0; pointer-events: none; stroke: var(--color-accent); stroke-width: 1.3; stroke-dasharray: 3 3; }
    .ovh-dot { opacity: 0; pointer-events: none; fill: var(--color-surface); stroke: var(--color-accent); stroke-width: 2; }
    .ovh-tip { opacity: 0; pointer-events: none; }
    .ovh:hover .ovh-cross, .ovh:hover .ovh-dot, .ovh:hover .ovh-tip { opacity: 1; }
    .ovh-tip-bg { fill: var(--color-text); }
    .ovh-tip-h { fill: var(--color-bg); font-size: 11px; font-weight: 800; }
    .ovh-tip-r { fill: var(--color-bg); font-size: 10.5px; }
    .ovh-tip-v { fill: var(--color-bg); font-size: 10.5px; font-family: var(--font-mono); }
    .agent-nums { color: var(--color-text-muted); display: flex; flex-wrap: wrap; font-family: var(--font-mono); font-size: 0.72rem; gap: var(--space-4); margin-top: var(--space-3); }
    .agent-nums strong { color: var(--color-text); font-weight: 700; }
    .ov-agent-usage-empty { color: var(--color-text-faint); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
    .agent-avatar.is-mini { border-radius: 7px; font-size: 0.58rem; height: 24px; width: 24px; }
    .agent-board-main { min-width: 0; }
    .ov-topsess { border-top: 1px solid var(--color-border); }
    .ov-topsess-head { color: var(--color-text-faint); font-size: 0.68rem; font-weight: 700; letter-spacing: 0.06em; padding: var(--space-4) 1rem var(--space-2); text-transform: uppercase; }
    .ov-topsess-row, .ov-sess-row { align-items: center; border-top: 1px solid var(--color-border); display: grid; gap: var(--space-4); grid-template-columns: 24px minmax(0, 1fr) auto; padding: var(--space-3) 1rem; }
    .ov-topsess-copy, .ov-sess-copy { min-width: 0; }
    .ov-topsess-copy strong, .ov-sess-copy strong { display: block; font-size: 0.8rem; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ov-topsess-copy span, .ov-sess-copy span { color: var(--color-text-faint); font-family: var(--font-mono); font-size: 0.68rem; }
    .ov-topsess-cost, .ov-sess-cost { font-family: var(--font-mono); font-size: 0.74rem; text-align: right; white-space: nowrap; }
    .ov-topsess-cost strong, .ov-sess-cost strong { font-weight: 700; }
    .ov-topsess-cost span, .ov-sess-cost span { color: var(--color-text-faint); display: block; font-size: 0.66rem; }
    .ov-task-sessions { border-top: 1px solid var(--color-border); margin: var(--space-5) -1rem 0; }
    .ov-task-sessions > summary { color: var(--color-accent); cursor: pointer; font-size: var(--font-note); font-weight: 700; list-style: none; min-height: 44px; padding: var(--space-4) 1rem; display: flex; align-items: center; }
    .ov-task-sessions > summary::-webkit-details-marker { display: none; }
    .ov-task-sessions > summary:focus-visible { box-shadow: inset 0 0 0 2px var(--color-accent); outline: 0; }
    .ov-task-sessions[open] { background: var(--color-surface-muted); }
    .ov-sess-scroll { background: var(--color-surface); border-top: 1px solid var(--color-border); }
    .ov-sess-cap { padding: var(--space-3) 1rem; }
    .control-hero { align-items: center; background: radial-gradient(circle at 90% 5%, rgba(45,212,191,0.24), transparent 34%), linear-gradient(135deg, #07111f, #12243d 58%, #123b3b); border-radius: 18px; color: white; display: grid; gap: 2rem; grid-template-columns: minmax(0, 1fr) auto; margin-bottom: var(--space-5); overflow: hidden; padding: 1.5rem; }
    .control-hero .eyebrow { color: #99f6e4; }
    .control-hero h2 { font-size: 1.65rem; margin-top: var(--space-2); }
    .control-hero p { color: #cbd5e1; line-height: 1.5; margin-top: var(--space-3); max-width: 720px; }
    .control-hero-stats { display: grid; gap: var(--space-3); grid-template-columns: repeat(3, minmax(84px, 1fr)); }
    .control-hero-stats div { background: rgba(255,255,255,0.09); border: 1px solid rgba(255,255,255,0.14); border-radius: var(--radius-lg); padding: var(--space-5); text-align: center; }
    .control-hero-stats strong, .control-hero-stats span { display: block; }
    .control-hero-stats strong { font-size: var(--font-stat); }
    .control-hero-stats span { color: #99f6e4; font-size: var(--font-xs); margin-top: var(--space-1); text-transform: uppercase; }
    .control-boundary { align-items: center; background: #ecfeff; border: 1px solid #a5f3fc; border-radius: var(--radius-lg); color: #155e75; display: flex; gap: var(--space-4); margin-bottom: var(--space-6); padding: var(--space-4) var(--space-5); }
    .control-boundary span { color: #475569; font-size: var(--font-note); }
    .control-notice { border: 1px solid var(--color-border); border-radius: var(--radius-lg); margin-bottom: var(--space-5); padding: var(--space-4) var(--space-5); }
    .control-notice.success { background: var(--status-ok-bg); border-color: #86efac; color: var(--status-ok-fg); }
    .control-notice.error { background: var(--status-error-bg); border-color: #fecaca; color: var(--status-error-fg); }
    .control-notice.info { background: var(--status-muted-bg); color: var(--color-text-secondary); }
    .control-task-form { display: grid; gap: var(--space-5); grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 0 var(--space-6) var(--space-6); }
    .control-task-form label { display: grid; gap: var(--space-2); }
    .control-task-form label > span { color: var(--color-text-secondary); font-size: var(--font-sm); font-weight: 800; }
    .control-task-form label > small { color: var(--color-text-faint); font-size: var(--font-xs); line-height: 1.4; }
    .control-task-form input, .control-task-form select, .control-task-form textarea { background: var(--color-surface); border: 1px solid var(--color-border-strong); border-radius: var(--radius-md); color: var(--color-text); font: inherit; min-height: 42px; padding: 0.65rem 0.75rem; width: 100%; }
    .control-task-form textarea { min-height: 92px; resize: vertical; }
    .control-wide { grid-column: 1 / -1; }
    .control-form-action { align-items: center; border-top: 1px solid var(--color-border); display: flex; gap: var(--space-5); justify-content: space-between; padding-top: var(--space-5); }
    .control-form-action p { color: var(--color-text-faint); font-size: var(--font-note); }
    .control-command-grid { display: grid; gap: var(--space-4); grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 0 var(--space-6) var(--space-5); }
    .control-command-grid > div { background: #0f172a; border-radius: var(--radius-lg); color: white; min-width: 0; padding: var(--space-5); }
    .control-command-grid strong, .control-command-grid code { display: block; }
    .control-command-grid code { color: #bfdbfe; font-size: var(--font-xs); margin-top: var(--space-3); overflow-wrap: anywhere; }
    .control-attempt-grid { display: grid; gap: var(--space-5); grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 0 var(--space-6) var(--space-6); }
    .control-attempt-card { border: 1px solid var(--color-border); border-radius: 12px; box-shadow: 0 7px 20px rgba(15,23,42,0.05); min-width: 0; overflow: hidden; }
    .control-attempt-head { align-items: flex-start; display: flex; gap: var(--space-5); justify-content: space-between; padding: var(--space-5); }
    .control-attempt-head h3 { font-size: 1rem; line-height: 1.35; margin: var(--space-2) 0 0; }
    .control-attempt-head p { color: var(--color-text-faint); font-size: var(--font-xs); margin-top: var(--space-2); }
    .control-axis { border-top: 1px solid var(--color-border); display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .control-axis > div { border-right: 1px solid var(--color-border); padding: var(--space-4); }
    .control-axis > div:last-child { border-right: 0; }
    .control-axis span:first-child { color: var(--color-text-faint); display: block; font-size: var(--font-2xs); margin-bottom: var(--space-2); text-transform: uppercase; }
    .control-attempt-action { align-items: center; background: var(--color-surface-muted); border-top: 1px solid var(--color-border); display: flex; min-height: 58px; padding: var(--space-4) var(--space-5); }
    .control-attempt-action form { margin: 0; }
    .control-empty { color: var(--color-text-muted); padding: 0 var(--space-6) var(--space-6); }
    .control-empty p { font-size: var(--font-note); margin-top: var(--space-2); }
    .control-two-column { display: grid; gap: var(--space-6); grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .control-list { list-style: none; margin: 0; padding: 0 var(--space-6) var(--space-6); }
    .control-list > li { align-items: center; border-top: 1px solid var(--color-border); display: grid; gap: var(--space-4); grid-template-columns: minmax(0, 1fr) auto; padding: var(--space-4) 0; }
    .control-list > li:first-child { border-top: 0; }
    .control-list strong, .control-list span { display: block; }
    .control-list span { color: var(--color-text-faint); font-size: var(--font-xs); margin-top: var(--space-1); }
    .control-inline-actions { display: flex; gap: var(--space-2); grid-column: 1 / -1; }
    .control-inline-actions form { margin: 0; }
    .control-list-empty { color: var(--color-text-faint); display: block !important; }
    @media (max-width: 720px) {
      main { padding: var(--space-5); }
      .activation-steps { grid-template-columns: 1fr; }
      .activation-step { min-height: 0; }
      .activation-action { align-items: stretch; flex-direction: column; }
      .hero { flex-direction: column; }
      .app-bar { align-items: flex-start; flex-direction: column; gap: var(--space-4); margin-bottom: 1.4rem; }
      .app-bar .actions { margin-left: 0; }
      .work-intro { flex-direction: column; }
      .work-intro-metrics { justify-content: flex-start; }
      .work-overview { gap: var(--space-6); grid-template-columns: 1fr; }
      .work-overview-stats { justify-content: space-between; width: 100%; }
      .work-overview-stat-stack { flex: 1; }
      .usage-pulse { grid-template-columns: 1fr; }
      .usage-pulse-copy { border-bottom: 1px solid var(--color-border); border-right: 0; }
      .usage-pulse-metrics { gap: var(--space-4); grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .usage-pulse-metric { border-right: 0; padding: 0; }
      .agent-board { grid-template-columns: 1fr; }
      .agent-board-copy { border-bottom: 1px solid var(--color-border); border-right: 0; }
      .task-brief-proof, .task-state-axis, .task-facts { grid-template-columns: 1fr; }
      .task-timeline-row { grid-template-columns: 1fr; }
      .task-lane-chip { justify-self: start; }
      .agent-roster { grid-template-columns: 1fr; }
      .agent-roster-item { border-bottom: 1px solid var(--color-border); border-right: 0; }
      .agent-roster-item:last-child { border-bottom: 0; }
      .work-feed { grid-template-columns: 1fr; }
      .work-feed-footer { align-items: flex-start; flex-direction: column; }
      .actions { justify-content: flex-start; }
      .identity-strip { grid-template-columns: 1fr; }
      .identity-cell { border-right: 0; border-top: 1px solid var(--color-border); }
      .identity-cell:first-child { border-top: 0; }
      .work-card-list { grid-template-columns: 1fr; }
      .work-card-facts { grid-template-columns: 1fr; }
      .work-fact { border-right: 0; border-top: 1px solid var(--color-border); }
      .work-fact:first-child { border-top: 0; }
      .control-hero { grid-template-columns: 1fr; }
      .control-hero-stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .control-boundary { align-items: flex-start; flex-direction: column; }
      .control-task-form, .control-command-grid, .control-attempt-grid, .control-two-column { grid-template-columns: 1fr; }
      .control-wide { grid-column: auto; }
      .control-form-action { align-items: stretch; flex-direction: column; }
      .control-axis { grid-template-columns: 1fr; }
      .control-axis > div { border-right: 0; border-top: 1px solid var(--color-border); }
      .control-axis > div:first-child { border-top: 0; }
    }
    @media (prefers-reduced-motion: reduce) {
      .run-step.is-active::before { animation: none; }
    }"""


def _esc_html(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


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


def _top_nav_html(active: str) -> str:
    active_group = _DASHBOARD_PAGE_GROUPS.get(active, active)
    links = []
    for page_id, href, label in _DASHBOARD_PAGES:
        css = "tab-link active" if page_id == active_group else "tab-link"
        current = ' aria-current="page"' if page_id == active_group else ""
        links.append(f'<a class="{css}" href="{href}"{current}>{label}</a>')
    return '<nav class="tabs" aria-label="Dashboard pages">' + "".join(links) + "</nav>"


def _footer_secondary_links_html() -> str:
    """Demoted links to the secondary surfaces (Advanced/Control) — kept
    reachable but out of the primary nav."""

    links = "".join(
        f'<a href="{href}">{_esc_html(label)}</a>' for href, label in _DASHBOARD_SECONDARY_PAGES
    )
    return f'<span class="footer-links-inner">{links}</span>'


def _instrumentation_cell_html(rollup_summary: dict[str, Any], esc: Any) -> str:
    """"Context after install" identity-strip cell (KPI semantics unchanged).

    The KPI only exists once a marker proves when recording was installed —
    without one the cell renders a muted setup hint, never a percentage (a
    fake 100% on an uninstrumented store is exactly the dishonesty markers
    exist to prevent).
    """

    instrumentation_summary = (
        rollup_summary.get("instrumentation") if isinstance(rollup_summary.get("instrumentation"), dict) else {}
    )
    instrumentation_kpi = (
        instrumentation_summary.get("post_context_kpi")
        if isinstance(instrumentation_summary.get("post_context_kpi"), dict)
        else {}
    )
    if not instrumentation_summary.get("markers_by_client"):
        return (
            '<div class="identity-cell log"><div class="label">Context after install</div>'
            '<div class="value">&mdash;</div>'
            '<div class="note">No instrumentation marker recorded &mdash; run `agentacct setup instructions` '
            "(or `setup mark-instrumented` to backfill).</div></div>"
        )
    kpi_post_sessions = int(instrumentation_kpi.get("post_sessions") or 0)
    kpi_post_with_context = int(instrumentation_kpi.get("post_with_context") or 0)
    kpi_rate = instrumentation_kpi.get("context_rate")
    # Floored, not rounded: only a true 1.0 may render 100% (199/200
    # rounding up to "100%" is the flattering direction).
    kpi_note = (
        f"{int(float(kpi_rate) * 100)}% of post-install top-level sessions have MCP context"
        if kpi_rate is not None
        else "no post-install top-level sessions yet"
    )
    return (
        '<div class="identity-cell log"><div class="label">Context after install</div>'
        f'<div class="value">{esc(_fmt_int(kpi_post_with_context))} of {esc(_fmt_int(kpi_post_sessions))}</div>'
        f'<div class="note">{esc(kpi_note)}</div></div>'
    )


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


def _identity_strip_html(
    *,
    ledger_overview: dict[str, Any],
    rollup_summary: dict[str, Any],
    store_label: str,
    store_scope: str,
    ingestion_health: Mapping[str, Any],
    esc: Any,
) -> str:
    """The which-store-am-I-looking-at bar rendered on every page (PRD §4)."""

    if store_scope == "project":
        scope_note = "project-scoped store"
    elif store_label == "All projects":
        scope_note = "machine-wide global store"
    else:
        scope_note = "custom-scoped store (--store-dir)"
    last_import = _fmt_time(ledger_overview.get("last_import_time"))
    last_import_html = esc(last_import) if last_import else '<span class="note">no saved imports yet</span>'
    saved_rows = int(ledger_overview.get("usage_truth_event_count") or 0)
    sync_state = str(ingestion_health.get("state") or "unknown")
    source_count = len(ingestion_health.get("sources") if isinstance(ingestion_health.get("sources"), list) else [])
    sync_issues = ingestion_health.get("issues") if isinstance(ingestion_health.get("issues"), list) else []
    source_repair_needed = any(
        isinstance(issue, Mapping)
        and str(issue.get("code") or "")
        in _ADVANCED_INGESTION_RECOVERY_CODES
        for issue in sync_issues
    )
    watcher = ingestion_health.get("watcher") if isinstance(ingestion_health.get("watcher"), Mapping) else {}
    watcher_state = str(watcher.get("state") or "not_configured")
    mechanical_projection = (
        rollup_summary.get("mechanical_projection")
        if isinstance(rollup_summary.get("mechanical_projection"), Mapping)
        else {}
    )
    if mechanical_projection.get("history_window_maybe_truncated"):
        sync_value = "Recent only"
        sync_note = "recent hook activity shown · complete evidence remains in Advanced"
    elif sync_state == "healthy":
        sync_value = "Live"
        sync_note = f"watcher running · {source_count} source(s) checked"
    elif sync_state == "degraded":
        sync_value = "Recovery needed"
        sync_note = (
            "Source repair needed · open Advanced recovery steps"
            if source_repair_needed
            else "Refresh now or inspect the source setup."
        )
    elif watcher_state == "running":
        sync_value = "Starting"
        sync_note = "watcher running · waiting for its first complete scan"
    elif source_count:
        sync_value = "Manual"
        sync_note = "last scan completed; continuous watcher is off"
    else:
        sync_value = "Ready"
        sync_note = "Refresh scans known local usage paths"
    return (
        '<div class="identity-strip">'
        f'<div class="identity-cell"><div class="label">Store</div><div class="value">{esc(store_label)}</div>'
        f'<div class="note">{esc(scope_note)}</div></div>'
        f'<div class="identity-cell"><div class="label">Last import</div><div class="value">{last_import_html}</div>'
        '<div class="note">Refresh &amp; save updates saved usage rows</div></div>'
        f'<div class="identity-cell"><div class="label">Saved usage rows</div><div class="value">{esc(_fmt_int(saved_rows))}</div>'
        '<div class="note">imported usage records in this store</div></div>'
        f'<div class="identity-cell"><div class="label">Activity sync</div><div class="value">{esc(sync_value)}</div>'
        f'<div class="note">{esc(sync_note)}</div></div>'
        f"{_instrumentation_cell_html(rollup_summary, esc)}"
        "</div>"
    )


def _ingestion_health_notice_html(ingestion_health: Mapping[str, Any], esc: Any) -> str:
    """Product copy for sync state: every non-live state includes a next step."""

    state = str(ingestion_health.get("state") or "unknown")
    sources = ingestion_health.get("sources") if isinstance(ingestion_health.get("sources"), list) else []
    issues = ingestion_health.get("issues") if isinstance(ingestion_health.get("issues"), list) else []
    watcher = ingestion_health.get("watcher") if isinstance(ingestion_health.get("watcher"), Mapping) else {}
    if state == "healthy":
        title = "Activity sync is live"
        detail = f"The watcher is running and the latest scan completed for {_fmt_int(len(sources))} source(s)."
        action_html = ""
    elif state == "degraded":
        title = "Activity sync needs recovery"
        first_issue = next(
            (
                issue
                for issue in issues
                if isinstance(issue, Mapping)
                and str(issue.get("code") or "")
                in _ADVANCED_INGESTION_RECOVERY_CODES
            ),
            next((issue for issue in issues if isinstance(issue, Mapping)), {}),
        )
        detail = str(first_issue.get("action") or "Refresh now or restart usage watch.")
        if (
            str(first_issue.get("code") or "")
            in _ADVANCED_INGESTION_RECOVERY_CODES
        ):
            action_html = (
                '<a class="button button-link" href="/advanced#activity-sync-recovery">'
                "Open recovery steps</a>"
            )
        else:
            action_html = (
                '<form method="post" action="/usage/import-local">'
                '<button class="button" type="submit">Refresh now</button></form>'
            )
    elif watcher.get("state") == "running":
        title = "Activity sync is starting"
        detail = "The watcher is running and waiting for its first complete scan of every configured source."
        action_html = ""
    elif sources:
        title = "Activity was refreshed manually"
        detail = "The last scan completed. Refresh again anytime, or open Advanced to set up continuous sync."
        action_html = (
            '<form method="post" action="/usage/import-local">'
            '<button class="button secondary" type="submit">Refresh again</button></form>'
        )
    else:
        title = "Activity sync is ready"
        detail = "Scan known local usage paths now; agentacct stores summarized usage only and never reads credentials."
        action_html = (
            '<form method="post" action="/usage/import-local">'
            '<button class="button" type="submit">Scan activity</button></form>'
        )
    return (
        f'<div class="sync-health {esc(state)}" role="status">'
        '<span class="sync-health-dot" aria-hidden="true"></span>'
        f'<div class="sync-health-copy"><strong>{esc(title)}</strong><span>{esc(detail)}</span></div>'
        f"{action_html}</div>"
    )


def _overview_freshness_html(data: _DashboardPageData, esc: Any) -> str:
    """Separate saved Work activity from source-scan freshness."""

    updated_candidates: list[float] = []

    def remember(value: Any) -> None:
        timestamp = _safe_float(value)
        if timestamp is not None and timestamp > 0:
            updated_candidates.append(timestamp)

    remember(data.ledger_overview.get("last_import_time"))
    for event in data.events:
        if isinstance(event, Mapping):
            remember(event.get("created_at"))
    for session in data.rollup_sessions:
        if isinstance(session, Mapping):
            remember(session.get("last_activity_at"))
    for item in data.work_items:
        if isinstance(item, Mapping):
            remember(item.get("updated_at"))

    updated = _fmt_time(max(updated_candidates)) if updated_candidates else "No saved activity yet"
    sources = (
        data.ingestion_health.get("sources")
        if isinstance(data.ingestion_health.get("sources"), list)
        else []
    )
    completions: list[float] = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for field in ("last_success_at", "last_failure_at"):
            timestamp = _safe_float(source.get(field))
            if timestamp is not None and timestamp > 0:
                completions.append(timestamp)
    last_checked = _fmt_time(max(completions)) if completions else ""
    checked_html = (
        f'<span><strong>Last checked</strong> {esc(last_checked)}</span>'
        if last_checked
        else '<span><strong>Last checked</strong> not yet</span>'
    )
    degraded_html = ""
    degraded = next(
        (
            source
            for source in sources
            if isinstance(source, Mapping) and str(source.get("state") or "") == "degraded"
        ),
        None,
    )
    if isinstance(degraded, Mapping):
        last_good = _fmt_time(degraded.get("last_success_at"))
        if last_good:
            degraded_html = (
                f'<span><strong>{esc(_human_client(degraded.get("source")))} last good</strong> '
                f'{esc(last_good)}</span>'
            )
    return (
        '<div class="overview-freshness" role="status" aria-label="Dashboard data freshness">'
        f'<span><strong>Last updated</strong> {esc(updated)}</span>{checked_html}{degraded_html}</div>'
    )


def _ingestion_health_panel_html(ingestion_health: Mapping[str, Any], esc: Any) -> str:
    sources = ingestion_health.get("sources") if isinstance(ingestion_health.get("sources"), list) else []
    watcher = ingestion_health.get("watcher") if isinstance(ingestion_health.get("watcher"), Mapping) else {}
    rows = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        unit = str(source.get("limit_unit") or "rows")
        if unit == "root_groups":
            selected_roots = int(source.get("selected_root_groups") or 0)
            limit_note = f"{_fmt_int(selected_roots)} root group(s) inspected"
            if source.get("returned_root_groups") is not None:
                returned_roots = int(source.get("returned_root_groups") or 0)
                if returned_roots != selected_roots:
                    limit_note += (
                        f" · {_fmt_int(returned_roots)} kept after global limit"
                    )
        elif unit == "transcript_files":
            selected_files = max(
                0,
                int(source.get("discovered") or 0) - int(source.get("excluded_by_limit") or 0),
            )
            limit_note = f"{_fmt_int(selected_files)} transcript(s) → {_fmt_int(source.get('returned_rows'))} row(s)"
        else:
            limit_note = f"{_fmt_int(source.get('returned_rows'))} row(s)"
        observed_sessions = int(source.get("observed_sessions") or 0)
        sessions_without_usage = int(source.get("sessions_without_usage") or 0)
        if observed_sessions:
            limit_note += f" · {_fmt_int(observed_sessions)} session(s) observed"
        if sessions_without_usage:
            limit_note += (
                f" · {_fmt_int(sessions_without_usage)} usage unavailable"
            )
        namespace_conflicts = int(source.get("source_namespace_conflicts") or 0)
        ignored_non_transcripts = int(source.get("ignored_non_transcript_files") or 0)
        if ignored_non_transcripts:
            limit_note += (
                f" · {_fmt_int(ignored_non_transcripts)} workflow journal(s) ignored"
            )
        transcript_candidates = max(
            0,
            int(source.get("discovered") or 0) - ignored_non_transcripts,
        )
        error_note = (
            f"{_fmt_int(namespace_conflicts)} source-home conflict(s)"
            if namespace_conflicts
            else str(source.get("error_code") or "none")
        )
        rows.append(
            "<tr>"
            f"<td><strong>{esc(_human_client(source.get('source')))}</strong></td>"
            f"<td><span class=\"status {_ledger_health_status_class(source.get('state'))}\">{esc(source.get('state'))}</span></td>"
            f"<td>{esc(_fmt_int(source.get('parsed')))} of {esc(_fmt_int(transcript_candidates))}</td>"
            f"<td>{esc(limit_note)}</td>"
            f"<td>{esc(str(source.get('scope') or 'manual').replace('_', ' ').title())}</td>"
            f"<td>{esc(_fmt_time(source.get('last_success_at')) or '—')}</td>"
            f"<td>{esc(error_note)}</td>"
            "</tr>"
        )
    rows_html = "".join(rows) or '<tr><td colspan="7">No source scan has completed yet.</td></tr>'
    watcher_state = str(watcher.get("state") or "not_configured").replace("_", " ").capitalize()
    issues = ingestion_health.get("issues") if isinstance(ingestion_health.get("issues"), list) else []
    source_repair_issue = next(
        (
            issue
            for issue in issues
            if isinstance(issue, Mapping)
            and str(issue.get("code") or "")
            in _ADVANCED_INGESTION_RECOVERY_CODES
        ),
        None,
    )
    recovery_html = ""
    if isinstance(source_repair_issue, Mapping):
        recovery_source = str(source_repair_issue.get("source") or "")
        recovery_code = str(source_repair_issue.get("code") or "")
        if recovery_source in SUPPORTED_CLIENTS:
            recovery_client = _human_client(recovery_source)
            retry_html = (
                f'<form method="post" action="/usage/import-local/{esc(recovery_source)}">'
                f'<button class="button" type="submit">Retry {esc(recovery_client)} after repair</button>'
                "</form>"
            )
            if recovery_code == "source_home_selection_required" and recovery_source == "hermes":
                inspect_html = (
                    "Choose and verify one home: <code>agentacct usage import-local --client hermes "
                    "--hermes-home /absolute/path/to/chosen-home --dry-run --json</code>"
                )
            elif recovery_source == "cursor":
                inspect_html = (
                    "Inspect the primary store only: <code>agentacct usage import-local --client cursor "
                    '--cursor-home "/absolute/path/to/Cursor" --dry-run --json</code>'
                )
            else:
                inspect_html = (
                    "Inspect first: <code>agentacct usage import-local --client "
                    f"{esc(recovery_source)} --dry-run --json</code>"
                )
            recovery_title = f"{recovery_client} source recovery"
        else:
            retry_html = ""
            inspect_html = "Inspect the affected source configuration before retrying its local import."
            recovery_title = "Activity source recovery"
        recovery_html = f"""<div class="sync-health degraded" id="activity-sync-recovery">
          <span class="sync-health-dot" aria-hidden="true"></span>
          <div class="sync-health-copy"><strong>{esc(recovery_title)}</strong><span>{esc(source_repair_issue.get('action'))}</span><span>{inspect_html}</span></div>
          {retry_html}
        </div>"""
    return f"""<section class="section" id="ingestion-health">
      <div class="section-header"><div><h2>Activity sync health</h2><p>Scanner receipts are separate from usage rows: a scan failure cannot fabricate activity or cost.</p></div><span class="status {_ledger_health_status_class(ingestion_health.get('state'))}">{esc(ingestion_health.get('state') or 'unknown')}</span></div>
      <div class="metric-grid">
        <div class="metric log"><div class="label">Watcher</div><div class="value">{esc(watcher_state)}</div><div class="note">lease and heartbeat state</div></div>
        <div class="metric bridge"><div class="label">Latest successful scan</div><div class="value">{esc(_fmt_time(ingestion_health.get('last_success_at')) or '—')}</div><div class="note">newest success across sources</div></div>
        <div class="metric"><div class="label">Active scans</div><div class="value">{esc(_fmt_int(ingestion_health.get('active_scan_count')))}</div><div class="note"><code>agentacct usage health --json</code></div></div>
      </div>
      <div class="table-wrap"><table><thead><tr><th>Source</th><th>State</th><th>Parsed selected / all candidates</th><th>Selection</th><th>Scope</th><th>Last success</th><th>Issue</th></tr></thead><tbody>{rows_html}</tbody></table></div>
      {recovery_html}
    </section>"""


def _page_doc(
    *,
    page_id: str,
    identity_html: str,
    body_html: str,
    notice_html: str = "",
    page_title: str | None = None,
    page_subtitle: str | None = None,
    footer_html: str = "agentacct local dashboard &mdash; localhost only. Costs shown anywhere are estimates or client-reported figures, never provider invoices.",
) -> str:
    """The ONE shared product shell and page layout."""

    page_group = _DASHBOARD_PAGE_GROUPS.get(page_id, page_id)
    default_title, default_subtitle = _DASHBOARD_PAGE_HEADERS.get(
        page_id,
        ("agentacct", "Local agent work and evidence."),
    )
    title_text = str(page_title or default_title)
    subtitle_text = str(page_subtitle or default_subtitle)
    if page_id == "advanced":
        secondary_action = '<a class="button secondary button-link" href="/raw">Preview local logs</a>'
    elif page_group == "advanced":
        secondary_action = '<a class="button secondary button-link" href="/advanced">Advanced home</a>'
    else:
        secondary_action = ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc_html(title_text)} · agentacct</title>
  <style>
{_DASHBOARD_STYLE}
  </style>
</head>
<body class="page-{page_id}">
  <a class="skip-link" href="#main-content">Skip to content</a>
  <main id="main-content" tabindex="-1">
    <header class="app-bar">
      <a class="brand" href="/"><span class="brand-mark">AC</span><span>agentacct</span></a>
      {_top_nav_html(page_id)}
      <div class="actions">
        <form method="post" action="/usage/import-local"><button class="button" type="submit">Refresh</button></form>
        {secondary_action}
      </div>
    </header>
    <section class="page-heading">
      <div class="eyebrow">Local agent activity</div>
      <h1>{_esc_html(title_text)}</h1>
      <p class="subtitle">{_esc_html(subtitle_text)}</p>
    </section>
    {identity_html}
    {notice_html}
{body_html}
    <footer class="footer"><span class="note">{footer_html}</span><span class="footer-links">{_footer_secondary_links_html()}</span></footer>
  </main>
</body>
</html>"""


@dataclass
class _DashboardPageData:
    """Everything the page renderers consume, computed from SAVED data only.

    ``usage_view.local_records`` is non-empty only when the caller (the /raw
    route) passed a live preview scan; product pages never pass one, which is
    what keeps the live client-log scan off their path (PRD §8).
    """

    esc: Any
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

    @property
    def identity_html(self) -> str:
        return _identity_strip_html(
            ledger_overview=self.ledger_overview,
            rollup_summary=self.rollup_summary,
            store_label=self.store_label,
            store_scope=self.store_scope,
            ingestion_health=self.ingestion_health,
            esc=self.esc,
        )


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
) -> _DashboardPageData:
    usage_view = _build_usage_view(list(local_usage_preview or []), events)
    # Built before any table renders: the rollup's collision-safe short
    # session labels are the ONE session-label source for every cell —
    # product pages AND raw page (full ids are href-only, locked decision).
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
        esc=_esc_html,
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


def _import_notice_html(import_status: dict[str, Any] | None, esc: Any) -> str:
    if import_status is None:
        return ""
    migrated_note = ""
    if import_status.get("migrated"):
        migrated_note = (
            f" {esc(_fmt_int(import_status.get('migrated', 0)))} record(s) replaced legacy per-model session keys "
            "(superseded keys kept in row provenance)."
        )
    namespace_note = ""
    if import_status.get("source_namespace_conflicts"):
        namespace_note = (
            f" agentacct protected {esc(_fmt_int(import_status.get('source_namespace_conflicts', 0)))} "
            "usage row(s) from a conflicting agent data home and did not overwrite them. "
            "Open Advanced, confirm that agent's configured data home, then restart sync."
        )
    adoption_note = ""
    if import_status.get("source_namespace_adoptions"):
        adoption_note = (
            f" {esc(_fmt_int(import_status.get('source_namespace_adoptions', 0)))} legacy row(s) "
            "adopted source-home provenance during this refresh."
        )
    concurrent_note = ""
    if import_status.get("concurrent_refresh_conflicts"):
        concurrent_note = (
            f" {esc(_fmt_int(import_status.get('concurrent_refresh_conflicts', 0)))} stale refresh row(s) "
            "were skipped because another refresh saved a newer revision first. Refresh again to inspect the latest totals."
        )
    incomplete_migration_note = ""
    if import_status.get("incomplete_alias_migrations"):
        incomplete_migration_note = (
            f" agentacct preserved {esc(_fmt_int(import_status.get('incomplete_alias_migrations', 0)))} "
            "legacy session(s) because this scan did not reproduce every stored model lane. "
            "Repair the client log or configured source path, then refresh again."
        )
    observation_note = ""
    if import_status.get("preserved_session_observations"):
        observation_note = (
            f" {esc(_fmt_int(import_status.get('preserved_session_observations', 0)))} real session(s) "
            "were preserved with usage unavailable; agentacct did not invent zero tokens or zero cost."
        )
    observation_conflict_note = ""
    if import_status.get("session_observation_conflicts"):
        observation_conflict_note = (
            f" agentacct quarantined {esc(_fmt_int(import_status.get('session_observation_conflicts', 0)))} "
            "ambiguous session-presence record(s) instead of joining them across source homes."
        )
    evidence_note = ""
    evidence_needs_attention = bool(
        import_status.get("evidence_reconcile_errors")
        or import_status.get("evidence_reconcile_conflicts")
        or (
            import_status.get("evidence_reconcile_enabled") is True
            and import_status.get("evidence_reconcile_complete") is False
        )
    )
    if evidence_needs_attention:
        evidence_note = (
            " Evidence v2 current-usage reconciliation needs attention; the saved local "
            "usage ledger remains intact. Retry usage refresh and inspect Advanced ingestion "
            "health before any Evidence rebuild or cleanup."
        )
    return (
        '<div class="notice">'
        "Activity refreshed. "
        f"{esc(_fmt_int(import_status.get('refreshed', import_status.get('imported', 0))))} saved usage record(s) updated; "
        f"{esc(_fmt_int(import_status.get('priced', 0)))} received list-price estimates."
        f"{migrated_note}"
        f"{namespace_note}"
        f"{adoption_note}"
        f"{concurrent_note}"
        f"{incomplete_migration_note}"
        f"{observation_note}"
        f"{observation_conflict_note}"
        f"{evidence_note}"
        "</div>"
    )


def _session_list_html(
    entries: list[dict[str, Any]], limit: int, esc: Any, *, empty_html: str | None = None
) -> tuple[str, str, dict[str, Any]]:
    """(rows html, cap note, capped payload) for a session-row list.

    ``empty_html`` overrides the default no-sessions-yet empty state — the
    /sessions explorer passes a filter-aware one that names the active
    filters (PRD §10.6: empty states say why and what to do next)."""

    capped = capped_rows(entries, limit)
    cap_note = _cap_note(capped["shown"], capped["total"], "top-level sessions")
    rows_html = "\n".join(_session_row_html(entry, esc) for entry in capped["rows"]) or empty_html or (
        '<div class="empty-state">No sessions yet. Import local usage (Refresh &amp; save usage) '
        "or record MCP sections during agent work to see sessions here.</div>"
    )
    return rows_html, cap_note, capped


def _cost_breakdown_sort_key(cost_sort: str) -> Any:
    def key(row: dict[str, Any]) -> Any:
        if cost_sort == "agent":
            return (str(row.get("client") or ""), str(row.get("provider") or ""), str(row.get("model") or ""))
        if cost_sort == "input":
            return row.get("input_cost_usd", 0.0)
        if cost_sort == "output":
            return row.get("output_cost_usd", 0.0)
        if cost_sort == "cache_create":
            return row.get("cache_creation_cost_usd", 0.0)
        if cost_sort == "cache_read":
            return row.get("cache_read_cost_usd", 0.0)
        return row.get("total_cost_usd", 0.0)

    return key


def _cost_breakdown_table_rows(rows: list[dict[str, Any]], esc: Any, empty_message: str) -> str:
    return "\n".join(
        "<tr>"
        f"<td>{esc(row.get('client'))}</td>"
        f"<td>{esc(_display_provider_model(row.get('provider'), row.get('model')))}</td>"
        f"<td>{_usage_record_count_cell(row, esc)}<br><span class=\"note\">{esc(_cost_coverage_label(row))}</span></td>"
        f"<td>{esc(_fmt_int(row.get('input_tokens')))}<br><span class=\"note\">{esc(_cost_breakdown_note(row, 'input_cost_usd'))}</span></td>"
        f"<td>{esc(_fmt_int(row.get('output_tokens')))}<br><span class=\"note\">{esc(_cost_breakdown_note(row, 'output_cost_usd'))}</span></td>"
        f"<td>{esc(_fmt_int(row.get('cache_creation_input_tokens')))}<br><span class=\"note\">{esc(_cost_breakdown_note(row, 'cache_creation_cost_usd'))}</span></td>"
        f"<td>{esc(_fmt_int(row.get('cache_read_input_tokens')))}<br><span class=\"note\">{esc(_cost_breakdown_note(row, 'cache_read_cost_usd'))}</span></td>"
        f"<td>{esc(_fmt_int(int(row.get('input_tokens') or 0) + int(row.get('output_tokens') or 0) + int(row.get('cached_input_tokens') or 0)))}<br><span class=\"note\">{esc(_cost_breakdown_note(row, 'total_cost_usd'))}</span></td>"
        "</tr>"
        for row in rows
    ) or f'<tr><td colspan="8">{esc(empty_message)}</td></tr>'


def _usage_over_time_table_parts(records: list[DashboardUsageRecord], esc: Any) -> tuple[str, str]:
    capped = capped_rows(_usage_over_time_rows(records), 30)
    cap_note = _cap_note(capped["shown"], capped["total"], "days")
    rows = "\n".join(
        "<tr>"
        f"<td>{esc(row.get('date'))}</td>"
        f"<td>{esc(_fmt_int(row.get('records')))}</td>"
        f"<td>{esc(_fmt_int(row.get('tokens')))}</td>"
        f"<td>{esc(_fmt_usd(row.get('cost_usd')))}</td>"
        "</tr>"
        for row in capped["rows"]
    ) or '<tr><td colspan="4">No saved usage over time yet.</td></tr>'
    return rows, cap_note


def _cost_confidence_table_html(records: list[DashboardUsageRecord], esc: Any) -> str:
    # The breakdown always emits its four fixed confidence buckets, so with
    # zero records the old `or` fallback never fired and an empty store
    # rendered four $0.00 rows — measured-looking zeros (PRD §10.6). Say
    # "nothing saved yet" instead.
    if not records:
        return '<tr><td colspan="4">No saved cost confidence rows yet.</td></tr>'
    return "\n".join(
        "<tr>"
        f"<td>{esc(row.get('confidence'))}</td>"
        f"<td>{esc(_fmt_int(row.get('records')))}</td>"
        f"<td>{esc(_fmt_int(row.get('tokens')))}</td>"
        f"<td>{esc(_fmt_usd(row.get('cost_usd')))}</td>"
        "</tr>"
        for row in _cost_confidence_breakdown(records, [])
    ) or '<tr><td colspan="4">No saved cost confidence rows yet.</td></tr>'


def _usage_confidence_table_html(records: list[DashboardUsageRecord], esc: Any) -> str:
    return "\n".join(
        "<tr>"
        f"<td>{esc(row.get('confidence'))}</td>"
        f"<td>{esc(_fmt_int(row.get('records')))}</td>"
        f"<td>{esc(_fmt_int(row.get('tokens')))}</td>"
        "</tr>"
        for row in _usage_confidence_breakdown(records)
    ) or '<tr><td colspan="3">No saved usage confidence rows yet.</td></tr>'


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
_CHART_PLOT_LEFT = 100.0
_CHART_PLOT_RIGHT = 712.0
_CHART_PLOT_TOP = 22.0
_CHART_BAR_BASELINE = 190.0
_CHART_XLABEL_Y = 206.0
_CHART_VIEWBOX = "0 0 720 230"
# One absurdly wide range (days=all + granularity=daily override) must not
# emit thousands of rects; the capped By period table below the chart is the
# fallback (both caps are stated in the chart's cap note).
_CHART_MAX_PERIODS = 120
# The By period table's own cap — named in the chart cap note, so the two
# notes can never disagree about what is shown.
_BY_PERIOD_TABLE_MAX = 60


def _nice_ceiling(value: int) -> int:
    """Smallest 1/2/5 x 10^k >= value — the chart's rounded y-axis max."""

    if value <= 1:
        return 1
    magnitude = 10 ** (len(str(int(value))) - 1)
    for multiplier in (1, 2, 5, 10):
        if value <= multiplier * magnitude:
            return multiplier * magnitude
    return 10 * magnitude


def _fmt_axis_value(value: float) -> str:
    if float(value).is_integer():
        return _fmt_int(int(value))
    return f"{value:,.1f}"


def _chart_period_label(period_key: str, granularity: str) -> str:
    if period_key == UNKNOWN_PERIOD:
        return "Unknown time"
    return f"Week of {period_key}" if granularity == "weekly" else period_key


def _bucket_confidence_label(bucket: dict[str, Any]) -> str:
    """Dominant-confidence pattern (PRD §10.2) for a cube bucket's cost.

    The label string is computed ONCE by the shared rule
    (usage_cube.dominant_cost_confidence) and shipped on the bucket as
    ``cost_confidence_label`` — every surface renders the same words."""

    return str(bucket.get("cost_confidence_label") or "no cost estimate")


def _total_bar_html(value: int, max_value: int, lane_class: str) -> str:
    """CSS ranked bar scaled on total reported token volume."""

    if max_value <= 0 or value <= 0:
        return ""
    width = max(1, round(value / max_value * 100))
    return f'<div class="bar"><span class="bar-fill {lane_class}" style="width:{width}%"></span></div>'


def _tokens_chart_html(
    cube: dict[str, Any],
    *,
    esc: Any,
    chart_id: str,
    granularity: str,
    range_label: str,
    range_days: int | None = None,
    today: date | None = None,
) -> str:
    """Server-side inline SVG chart (PRD §5.3) — renders under the unchanged
    CSP, zero JS.

    Honesty as marks: bar height is TOTAL reported token volume, including
    input, output, cache writes, and cache reads. Bars are stacked by client
    only; native per-rect tooltips expose the four-part category breakdown.
    A source that omits cache-write telemetry keeps those unclassified tokens
    inside input, so the total remains correct without inventing a zero write.
    Accessibility: role="img" + aria-labelledby title/desc stating the basis
    in words, empty periods rendered as labeled hover slots, and the By period
    table nearby as the capped fallback.

    ``range_days``/``today`` (the caller's single per-request today) let the
    weekly view mark the leading partial bucket: when the range starts
    mid-week, that bucket's hover says so instead of claiming "no usage
    rows" for a calendar week the filter truncated.
    """

    periods = [entry for entry in cube["by_period"] if entry.get("period") != UNKNOWN_PERIOD]
    rows_in_filter = int(cube["totals"].get("rows") or 0)
    additive_rows = int(cube["totals"].get("additive_rows") or 0)
    excluded_rows = int(cube["totals"].get("excluded_non_additive_rows") or 0)
    if excluded_rows and not additive_rows:
        return (
            f'<figure class="chart" id="{esc(chart_id)}"><div class="empty-state">'
            f'{esc(_fmt_int(excluded_rows))} saved usage row(s) match this filter, '
            'but their trustworthy additive usage is unavailable until source identity or lineage normalization. No zero-token chart is drawn.</div></figure>'
        )
    if not periods or rows_in_filter == 0:
        if rows_in_filter > 0:
            # Rows ARE saved and in this range (days=all keeps unknown-time
            # rows in the totals) — none carries a usable timestamp, so there
            # is nothing to chart. Never claim the rows don't exist.
            message = (
                f"{esc(_fmt_int(rows_in_filter))} saved usage row(s) match this filter, but none carries a "
                "usable timestamp to chart. They are listed under Unknown time in the By period table below."
            )
        else:
            message = (
                "No saved usage rows in this range yet. Use Refresh &amp; save usage to import local "
                "client logs, or widen the date range."
            )
        return f'<figure class="chart" id="{esc(chart_id)}"><div class="empty-state">{message}</div></figure>'
    cap_note = ""
    if len(periods) > _CHART_MAX_PERIODS:
        # Both caps stated over the SAME dated-period total — the table pins
        # its Unknown time row outside its cap, so neither note is false.
        cap_note = (
            f" Chart draws the most recent {_fmt_int(_CHART_MAX_PERIODS)} of {_fmt_int(len(periods))} periods; "
            f"the By period table below shows the most recent {_fmt_int(_BY_PERIOD_TABLE_MAX)} of "
            f"{_fmt_int(len(periods))}."
        )
        periods = periods[-_CHART_MAX_PERIODS:]
    partial_leading_key = None
    if granularity == "weekly" and range_days is not None:
        range_start = (today or date.today()) - timedelta(days=range_days - 1)
        if week_start(range_start) < range_start:
            partial_leading_key = week_start(range_start).isoformat()
    clients = [str(entry.get("client") or "") for entry in cube["by_client"]]
    unit = "week" if granularity == "weekly" else "day"

    stack_max = max((int(entry.get("total_tokens_including_cached") or 0) for entry in periods), default=0)
    y_max = _nice_ceiling(stack_max)
    plot_height = _CHART_BAR_BASELINE - _CHART_PLOT_TOP
    slot = (_CHART_PLOT_RIGHT - _CHART_PLOT_LEFT) / len(periods)
    bar_width = max(1.0, slot * 0.7)

    parts: list[str] = []
    # A zero-token range measured nothing on the total axis: render the flat
    # baseline alone — a fabricated 0-1 scale
    # with a fractional "0.5" tick would look like a measurement.
    if stack_max > 0:
        for fraction in (1.0, 0.5):
            grid_y = _CHART_BAR_BASELINE - fraction * plot_height
            parts.append(
                f'<line class="chart-grid" x1="{_CHART_PLOT_LEFT}" y1="{grid_y:.1f}" x2="{_CHART_PLOT_RIGHT}" y2="{grid_y:.1f}"></line>'
            )
            parts.append(
                f'<text class="chart-axis-label" x="{_CHART_PLOT_LEFT - 6:.1f}" y="{grid_y + 4:.1f}" text-anchor="end">{esc(_fmt_axis_value(y_max * fraction))}</text>'
            )
    parts.append(
        f'<line class="chart-grid" x1="{_CHART_PLOT_LEFT}" y1="{_CHART_BAR_BASELINE}" x2="{_CHART_PLOT_RIGHT}" y2="{_CHART_BAR_BASELINE}"></line>'
    )
    parts.append(
        f'<text class="chart-axis-label" x="{_CHART_PLOT_LEFT - 6:.1f}" y="{_CHART_BAR_BASELINE + 4:.1f}" text-anchor="end">0</text>'
    )

    label_step = max(1, len(periods) // 6)
    # Instant no-JS hover tooltips (same pattern as the homepage charts):
    # collected separately and painted AFTER every bar so a tall stack never
    # occludes a neighbor's tooltip. The verbose per-rect <title> stays for
    # assistive tech and the By-period table remains the capped fallback.
    hover_parts: list[str] = []
    for index, entry in enumerate(periods):
        period_key = str(entry.get("period"))
        label = _chart_period_label(period_key, granularity)
        x = _CHART_PLOT_LEFT + index * slot + (slot - bar_width) / 2
        stack_y = _CHART_BAR_BASELINE
        tip_rows: list[tuple[str | None, str, str]] = []
        lanes = entry.get("by_client") or {}
        for client in clients:
            lane = lanes.get(client)
            if not isinstance(lane, dict):
                continue
            value = int(lane.get("total_tokens_including_cached") or 0)
            if value <= 0:
                continue
            height = value / y_max * plot_height
            stack_y -= height
            write_reporting = str(lane.get("cache_creation_reporting") or "")
            write_value = _fmt_int(lane.get("cache_creation_tokens"))
            if write_reporting == "not_reported":
                write_detail = "cache writes not reported; unclassified writes remain in input"
            elif write_reporting == "unknown":
                write_detail = "cache-write reporting capability unknown; unclassified writes remain in input"
            elif write_reporting == "partial":
                write_detail = f"{write_value} reported cache writes (partial coverage)"
            else:
                write_detail = f"{write_value} cache writes"
            read_reporting = str(lane.get("cache_read_reporting") or "")
            read_value = _fmt_int(lane.get("cache_read_tokens"))
            if read_reporting == "not_reported":
                read_detail = "cache reads not reported; unclassified reads remain in input"
            elif read_reporting == "unknown":
                read_detail = "cache-read reporting capability unknown; unclassified reads remain in input"
            elif read_reporting == "partial":
                read_detail = f"{read_value} reported cache reads (partial coverage)"
            else:
                read_detail = f"{read_value} cache reads"
            detail = (
                f"{label} · {_human_client(client)} · {_fmt_int(value)} total tokens · "
                f"{_fmt_int(lane.get('input_tokens'))} input after reported cache · "
                f"{_fmt_int(lane.get('output_tokens'))} output · {write_detail} · {read_detail}"
            )
            parts.append(
                f'<rect class="chart-bar {client_lane_class(client)}" x="{x:.1f}" y="{stack_y:.1f}" width="{bar_width:.1f}" height="{height:.1f}">'
                f"<title>{esc(detail)}</title></rect>"
            )
            tip_rows.append(
                (f"fill:var(--{client_lane_class(client)})", _human_client(client), _fmt_compact_int(value))
            )
        period_total = int(entry.get("total_tokens_including_cached") or 0)
        if int(entry.get("rows") or 0) == 0:
            # Empty periods stay visible (a gap is information): a labeled
            # transparent hover slot, never a fabricated bar. The leading
            # partial weekly bucket must not claim "no usage rows" for a
            # calendar week the range only partially covers.
            if period_key == partial_leading_key:
                slot_note = "partial week (range starts mid-week)"
            else:
                slot_note = "no usage rows"
            parts.append(
                f'<rect class="chart-hover" x="{x:.1f}" y="{_CHART_PLOT_TOP}" width="{bar_width:.1f}" height="{plot_height:.1f}">'
                f"<title>{esc(label)} · {esc(slot_note)}</title></rect>"
            )
            hover_tip_rows: list[tuple[str | None, str, str]] = [(None, slot_note, "")]
            hover_a11y = f"{label} · {slot_note}"
        else:
            if len(tip_rows) > 7:
                hidden = len(tip_rows) - 7
                tip_rows = tip_rows[:7] + [(None, f"+{_fmt_int(hidden)} more", "")]
            hover_tip_rows = tip_rows + [(None, "Total", _fmt_compact_int(period_total))]
            hover_a11y = f"{label} · {_fmt_compact_int(period_total)} tokens total"
        hover_parts.append(
            _ov_hover_column(
                slot_x=_CHART_PLOT_LEFT + index * slot,
                slot_width=slot,
                cx=_CHART_PLOT_LEFT + index * slot + slot / 2,
                plot_top=_CHART_PLOT_TOP,
                plot_base=_CHART_BAR_BASELINE,
                plot_left=_CHART_PLOT_LEFT,
                plot_right=_CHART_PLOT_RIGHT,
                header=label,
                rows=hover_tip_rows,
                a11y=hover_a11y,
                esc=esc,
            )
        )
        if index % label_step == 0:
            parts.append(
                f'<text class="chart-axis-label" x="{x + bar_width / 2:.1f}" y="{_CHART_XLABEL_Y}" text-anchor="middle">{esc(period_key[5:])}</text>'
            )
    parts.extend(hover_parts)

    chart_title = f"Total tokens per {unit}, {range_label}, stacked by platform"
    chart_desc = (
        "Each bar includes every reported input, output, cache-write, and cache-read token, stacked by platform. "
        "Tooltips show the category breakdown and identify missing or unknown cache counters. "
        "Values come from saved usage rows only."
    )
    legend_chips = [
        f'<span class="chip"><span class="legend-swatch {client_lane_class(client)}"></span>{esc(_human_client(client))}</span>'
        for client in clients
    ]
    return (
        f'<figure class="chart" id="{esc(chart_id)}">'
        f'<svg viewBox="{_CHART_VIEWBOX}" role="img" aria-labelledby="{esc(chart_id)}-title {esc(chart_id)}-desc" preserveAspectRatio="xMidYMid meet">'
        f'<title id="{esc(chart_id)}-title">{esc(chart_title)}</title>'
        f'<desc id="{esc(chart_id)}-desc">{esc(chart_desc)}</desc>'
        + "".join(parts)
        + "</svg>"
        f'<figcaption class="chart-legend">{" ".join(legend_chips)}<span class="note"> Bar height = all reported token categories.{esc(cap_note)}</span></figcaption>'
        "</figure>"
    )


# ---------------------------------------------------------------------------
# Overview usage charts (Dashboard v2, Stage 2). Server-rendered inline SVG,
# zero JS (the dashboard CSP forbids script). Hover detail is native SVG
# <title>; the daily breakdown selector is a query-param link that re-renders
# the whole page. Honesty as marks: bar/line height is TOTAL reported tokens
# including cache; held (non-additive) usage rows are disclosed in words, never
# summed into the picture and never drawn as a fabricated zero.
# ---------------------------------------------------------------------------

_OV_CHART_LEFT = 8.0
_OV_CHART_RIGHT = 712.0
_OV_LINE_TOP = 12.0
_OV_LINE_BASE = 128.0
_OV_LINE_VIEWBOX = "0 0 720 150"
# The bar chart carries a y-axis, so it needs a left gutter wide enough for a
# compact label ("24.4B"); the line chart has no axis labels and spans full
# width from _OV_CHART_LEFT.
_OV_BAR_LEFT = 52.0
_OV_BAR_TOP = 10.0
_OV_BAR_BASE = 150.0
_OV_BAR_XLABEL_Y = 168.0
_OV_BAR_VIEWBOX = "0 0 720 176"
# Model / agent-model lanes keep their owning agent's hue; sub-models within one
# client step opacity so two Claude models are still distinguishable without
# inventing a fresh color per model (matches the approved preview).
_OV_LANE_OPACITY_STEPS = (1.0, 0.62, 0.44, 0.34, 0.28, 0.24)
# Cap the stacked-breakdown series so the legend and stack stay legible; the
# tail folds into one honestly labeled "Other models" lane rather than being
# silently dropped.
_OV_MAX_BREAKDOWN_SERIES = 8


def _ov_lane_opacity(rank: int) -> float:
    steps = _OV_LANE_OPACITY_STEPS
    return steps[rank] if 0 <= rank < len(steps) else steps[-1]


def _ov_hover_column(
    *,
    slot_x: float,
    slot_width: float,
    cx: float,
    plot_top: float,
    plot_base: float,
    plot_left: float,
    plot_right: float,
    header: str,
    rows: list[tuple[str | None, str, str]],
    a11y: str,
    esc: Any,
    marks: str = "",
) -> str:
    """One day's hover zone for a no-JS chart. Emits a full-height transparent
    hit rect, a crosshair, and a styled tooltip (date header + per-series/value
    rows) that a CSS ``:hover`` rule reveals the instant the pointer enters the
    column — no JavaScript, so it works under the dashboard's script-free CSP.
    ``rows`` items are ``(swatch_css_or_None, label, value)``; ``a11y`` is a
    concise native ``<title>`` for assistive tech and touch."""

    box_w = 178.0
    pad = 6.0
    line_h = 14.0
    head_h = 16.0
    # Callers bound their own row count (e.g. the bar chart folds a long series
    # list into "+K more" so the Total row is never dropped); this cap is only a
    # safety net that keeps the box inside the viewBox.
    visible = rows[:9]
    box_h = pad * 2 + head_h + line_h * len(visible)
    tip_x = min(max(cx - box_w / 2, plot_left), plot_right - box_w)
    tip_y = plot_top + 3.0
    parts = [
        f'<rect class="ovh-hit" x="{slot_x:.1f}" y="{plot_top:.1f}" width="{slot_width:.1f}" '
        f'height="{plot_base - plot_top:.1f}"><title>{esc(a11y)}</title></rect>',
        f'<line class="ovh-cross" x1="{cx:.1f}" y1="{plot_top:.1f}" x2="{cx:.1f}" y2="{plot_base:.1f}"></line>',
        marks,
        f'<g class="ovh-tip" transform="translate({tip_x:.1f},{tip_y:.1f})">',
        f'<rect class="ovh-tip-bg" x="0" y="0" width="{box_w:.1f}" height="{box_h:.1f}" rx="7"></rect>',
        f'<text class="ovh-tip-h" x="{pad:.1f}" y="{pad + 11:.1f}">{esc(header)}</text>',
    ]
    text_y = pad + head_h + 10.5
    for swatch, label, value in visible:
        if swatch:
            parts.append(
                f'<rect class="ovh-sw" x="{pad:.1f}" y="{text_y - 8:.1f}" width="8" height="8" rx="2" '
                f'style="{swatch}"></rect>'
            )
            label_x = pad + 13.0
        else:
            label_x = pad
        parts.append(
            f'<text class="ovh-tip-r" x="{label_x:.1f}" y="{text_y:.1f}">{esc(_short_text(label, max_length=16))}</text>'
        )
        if value:
            parts.append(
                f'<text class="ovh-tip-v" x="{box_w - pad:.1f}" y="{text_y:.1f}" text-anchor="end">{esc(value)}</text>'
            )
        text_y += line_h
    parts.append("</g>")
    return '<g class="ovh">' + "".join(parts) + "</g>"


def _overview_usage_line_html(
    days: list[tuple[str, int, int]],
    *,
    esc: Any,
    chart_id: str,
    held_rows: int,
    range_label: str,
    measured_days: int,
) -> str:
    """Cumulative total-token line (no JS). ``days`` is ascending
    ``(period_iso, rows, daily_total_incl_cache)``; the line plots the running
    sum. Empty/held windows render an explicit labeled state, never a zero
    line. ``measured_days`` is the count of days that actually carried usage
    rows (the axis is gap-filled to the whole range, so the accessible title
    must NOT call every empty calendar day 'measured')."""

    dated = [(str(period), int(rows or 0), int(total or 0)) for period, rows, total in days]
    grand_total = sum(total for _period, _rows, total in dated)
    if not dated or grand_total <= 0:
        if held_rows:
            message = (
                f"{esc(_fmt_int(held_rows))} saved usage row(s) match this window, but their "
                "trustworthy additive usage is unavailable until source identity or lineage "
                "normalization. No zero-token line is drawn."
            )
        else:
            message = (
                "No saved usage rows in this window yet. Use Refresh &amp; save usage to import "
                "local client logs."
            )
        return f'<figure class="chart ov-linechart" id="{esc(chart_id)}"><div class="empty-state">{message}</div></figure>'
    left, right, top, base = _OV_CHART_LEFT, _OV_CHART_RIGHT, _OV_LINE_TOP, _OV_LINE_BASE
    plot_height = base - top
    count = len(dated)
    cumulative: list[int] = []
    running = 0
    for _period, _rows, total in dated:
        running += total
        cumulative.append(running)
    y_max = cumulative[-1] or 1
    xs = [left + (index / (count - 1) if count > 1 else 0.0) * (right - left) for index in range(count)]
    ys = [base - (cumulative[index] / y_max) * plot_height for index in range(count)]
    grid = "".join(
        f'<line class="chart-grid" x1="{left:.1f}" y1="{base - fraction * plot_height:.1f}" '
        f'x2="{right:.1f}" y2="{base - fraction * plot_height:.1f}"></line>'
        for fraction in (0.25, 0.5, 0.75, 1.0)
    )
    if count == 1:
        line_path = f"M{xs[0]:.1f},{ys[0]:.1f} L{right:.1f},{ys[0]:.1f}"
    else:
        line_path = "M" + " L".join(f"{xs[index]:.1f},{ys[index]:.1f}" for index in range(count))
    # Close the area on the plot's own left/right edges (constants), so the
    # single-point/horizontal case fills the full region under the line rather
    # than a degenerate triangle.
    area_path = line_path + f" L{right:.1f},{base:.1f} L{left:.1f},{base:.1f} Z"
    # Use the inter-point spacing (not range/count) so the centered hover zones
    # tile edge-to-edge with no dead gaps between days.
    col_width = (right - left) / (count - 1) if count > 1 else (right - left)
    hover = "".join(
        _ov_hover_column(
            slot_x=xs[index] - col_width / 2,
            slot_width=col_width,
            cx=xs[index],
            plot_top=top,
            plot_base=base,
            plot_left=left,
            plot_right=right,
            header=dated[index][0],
            rows=[
                (None, "This day", f"+{_fmt_compact_int(dated[index][2])}"),
                (None, "Cumulative", _fmt_compact_int(cumulative[index])),
            ],
            a11y=(
                f"{dated[index][0]} · +{_fmt_compact_int(dated[index][2])} that day · "
                f"{_fmt_compact_int(cumulative[index])} cumulative"
            ),
            esc=esc,
            marks=f'<circle class="ovh-dot" cx="{xs[index]:.1f}" cy="{ys[index]:.1f}" r="3.4"></circle>',
        )
        for index in range(count)
    )
    grad = (
        f'<defs><linearGradient id="{esc(chart_id)}-fill" x1="0" y1="0" x2="0" y2="1">'
        f'<stop class="ov-line-g0" offset="0"></stop>'
        f'<stop class="ov-line-g1" offset="1"></stop></linearGradient></defs>'
    )
    endpoint = f'<circle class="ov-line-dot" cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3.2"></circle>'
    held_note = f" {_fmt_int(held_rows)} usage row(s) held from totals." if held_rows else ""
    title = f"Cumulative total tokens, {range_label}"
    return (
        f'<figure class="chart ov-linechart" id="{esc(chart_id)}">'
        f'<svg viewBox="{_OV_LINE_VIEWBOX}" role="img" aria-labelledby="{esc(chart_id)}-t" preserveAspectRatio="none">'
        f'<title id="{esc(chart_id)}-t">{esc(title)}: {esc(_fmt_compact_int(y_max))} tokens across '
        f'{esc(_fmt_int(measured_days))} measured {"day" if measured_days == 1 else "days"} '
        f'in the {esc(range_label)}.</title>'
        f'{grad}{grid}'
        f'<path class="ov-line-area" d="{area_path}" style="fill:url(#{esc(chart_id)}-fill)"></path>'
        f'<path class="ov-line-path" d="{line_path}"></path>'
        f'{endpoint}{hover}'
        "</svg>"
        f'<figcaption class="chart-legend"><span class="note">Running total of all reported tokens '
        f'incl. cache, {esc(range_label)}.{esc(held_note)}</span></figcaption>'
        "</figure>"
    )


def _overview_daily_stack_html(
    days: list[tuple[str, int]],
    series: list[dict[str, Any]],
    *,
    esc: Any,
    chart_id: str,
    held_rows: int,
    range_label: str,
    breakdown_label: str,
) -> str:
    """Daily stacked-bar chart (no JS). ``days`` is ascending
    ``(period_iso, rows)``; ``series`` is stacking order (largest first), each
    ``{label, fill, op, per_day: {iso: {total,input,output,cache_creation,
    cache_read}}}``. Bar height = total tokens incl. cache; empty days stay
    visible as labeled hover slots; held rows are disclosed, never charted."""

    day_keys = [str(period) for period, _rows in days]
    day_rows = {str(period): int(rows or 0) for period, rows in days}
    day_total: dict[str, int] = {key: 0 for key in day_keys}
    for entry in series:
        for key, lane in entry["per_day"].items():
            if key in day_total:
                day_total[key] += int(lane.get("total") or 0)
    stack_max = max(day_total.values(), default=0)
    if not day_keys or stack_max <= 0:
        if held_rows:
            message = (
                f"{esc(_fmt_int(held_rows))} saved usage row(s) match this window, but their additive "
                "usage is unavailable until source identity or lineage normalization. No zero-token "
                "chart is drawn."
            )
        else:
            message = (
                "No saved usage rows in this window yet. Use Refresh &amp; save usage to import local "
                "client logs."
            )
        return f'<figure class="chart ov-barchart" id="{esc(chart_id)}"><div class="empty-state">{message}</div></figure>'
    y_max = _nice_ceiling(stack_max)
    left, right, top, base = _OV_BAR_LEFT, _OV_CHART_RIGHT, _OV_BAR_TOP, _OV_BAR_BASE
    plot_height = base - top
    count = len(day_keys)
    slot = (right - left) / count
    bar_width = max(1.0, slot * 0.72)
    parts: list[str] = []
    for fraction in (1.0, 0.5):
        grid_y = base - fraction * plot_height
        parts.append(
            f'<line class="chart-grid" x1="{left:.1f}" y1="{grid_y:.1f}" x2="{right:.1f}" y2="{grid_y:.1f}"></line>'
        )
        parts.append(
            f'<text class="chart-axis-label" x="{left - 6:.1f}" y="{grid_y + 4:.1f}" text-anchor="end">'
            f'{esc(_fmt_compact_int(int(y_max * fraction)))}</text>'
        )
    parts.append(f'<line class="chart-grid" x1="{left:.1f}" y1="{base:.1f}" x2="{right:.1f}" y2="{base:.1f}"></line>')
    parts.append(f'<text class="chart-axis-label" x="{left - 6:.1f}" y="{base + 4:.1f}" text-anchor="end">0</text>')
    label_step = max(1, count // 8)
    # Hover columns are collected separately and appended AFTER every bar, so a
    # day's tooltip always paints above neighboring bars (a tall spike must not
    # occlude an earlier column's tooltip).
    hover_parts: list[str] = []
    for index, key in enumerate(day_keys):
        x = left + index * slot + (slot - bar_width) / 2
        stack_y = base
        tip_rows: list[tuple[str | None, str, str]] = []
        for entry in series:
            lane = entry["per_day"].get(key)
            if not lane:
                continue
            value = int(lane.get("total") or 0)
            if value <= 0:
                continue
            height = value / y_max * plot_height
            stack_y -= height
            style = f'fill:{entry["fill"]}'
            opacity = float(entry.get("op") or 1.0)
            if opacity < 1.0:
                style += f";opacity:{opacity:.2f}"
            parts.append(
                f'<rect class="chart-bar" x="{x:.1f}" y="{stack_y:.1f}" width="{bar_width:.1f}" '
                f'height="{height:.1f}" style="{style}"></rect>'
            )
            tip_rows.append((style, entry["label"], _fmt_compact_int(value)))
        day_tot = int(day_total.get(key, 0))
        if tip_rows:
            # Keep the tooltip bounded without ever dropping the Total: fold a
            # long series list into a "+K more" row so the caller, not the
            # helper's safety cap, decides what is summarized.
            max_series = 7
            if len(tip_rows) > max_series:
                hidden = len(tip_rows) - max_series
                tip_rows = tip_rows[:max_series] + [(None, f"+{_fmt_int(hidden)} more", "")]
            tip_rows.append((None, "Total", _fmt_compact_int(day_tot)))
            a11y = f"{key} · {_fmt_compact_int(day_tot)} tokens total"
        else:
            # A day that drew no bar still gets a labeled hover column: either it
            # is truly empty, or its rows are all held (non-additive), so there
            # is nothing additive to chart — never a silent blank column.
            rows_here = int(day_rows.get(key, 0))
            if rows_here:
                tip_rows = [(None, f"{_fmt_int(rows_here)} row(s) held", "")]
                a11y = f"{key} · {_fmt_int(rows_here)} usage row(s), no additive tokens to chart"
            else:
                tip_rows = [(None, "No usage rows", "")]
                a11y = f"{key} · no usage rows"
        hover_parts.append(
            _ov_hover_column(
                slot_x=left + index * slot,
                slot_width=slot,
                cx=left + index * slot + slot / 2,
                plot_top=top,
                plot_base=base,
                plot_left=left,
                plot_right=right,
                header=key,
                rows=tip_rows,
                a11y=a11y,
                esc=esc,
            )
        )
        if index % label_step == 0:
            parts.append(
                f'<text class="chart-axis-label" x="{x + bar_width / 2:.1f}" y="{_OV_BAR_XLABEL_Y}" '
                f'text-anchor="middle">{esc(key[5:])}</text>'
            )
    parts.extend(hover_parts)
    legend_chips: list[str] = []
    for entry in series:
        swatch_style = f'background:{entry["fill"]}'
        opacity = float(entry.get("op") or 1.0)
        if opacity < 1.0:
            swatch_style += f";opacity:{opacity:.2f}"
        legend_chips.append(
            f'<span class="chip"><span class="legend-swatch" style="{swatch_style}"></span>{esc(entry["label"])}</span>'
        )
    held_note = f" {_fmt_int(held_rows)} usage row(s) held from these totals." if held_rows else ""
    title = f"Daily total tokens, {range_label}, stacked {breakdown_label}"
    desc = (
        "Each bar totals every reported input, output, and cache token for that day. Tooltips give the "
        "per-series breakdown. Held rows are disclosed and excluded, never drawn as zero."
    )
    return (
        f'<figure class="chart ov-barchart" id="{esc(chart_id)}">'
        f'<svg viewBox="{_OV_BAR_VIEWBOX}" role="img" aria-labelledby="{esc(chart_id)}-t {esc(chart_id)}-d" '
        f'preserveAspectRatio="xMidYMid meet">'
        f'<title id="{esc(chart_id)}-t">{esc(title)}.</title>'
        f'<desc id="{esc(chart_id)}-d">{esc(desc)}</desc>'
        + "".join(parts)
        + "</svg>"
        f'<figcaption class="chart-legend">{" ".join(legend_chips)}'
        f'<span class="note"> Bar height = all reported tokens incl. cache.{esc(held_note)}</span></figcaption>'
        "</figure>"
    )


def _overview_breakdown_series(
    cube_30d: dict[str, Any],
    scoped_records: list[DashboardUsageRecord],
    *,
    breakdown: str,
    today: date,
) -> tuple[list[tuple[str, int, int]], list[tuple[str, int]], list[dict[str, Any]]]:
    """Build ``(line_days, bar_days, stack_series)`` for the Overview usage
    charts from one scoped 30-day daily cube.

    ``agent`` reuses the cube's per-client period lanes (identical day buckets
    and totals as the line + tiles). ``model`` / ``agent-model`` re-aggregate
    the same scoped records keyed on model / (client, model), honoring
    ``usage_additive`` exactly like the cube, colored by the owning agent's
    lane with a stepped opacity per sub-model and an honest ``Other models``
    tail when the series count exceeds the cap."""

    dated_periods = [
        entry for entry in cube_30d["by_period"] if entry.get("period") != UNKNOWN_PERIOD
    ]
    line_days = [
        (str(entry.get("period")), int(entry.get("rows") or 0), int(entry.get("total_tokens_including_cached") or 0))
        for entry in dated_periods
    ]
    bar_days = [(str(entry.get("period")), int(entry.get("rows") or 0)) for entry in dated_periods]

    def lane_slot(lane: Mapping[str, Any]) -> dict[str, int]:
        return {
            "total": int(lane.get("total_tokens_including_cached") or 0),
            "input": int(lane.get("input_tokens") or 0),
            "output": int(lane.get("output_tokens") or 0),
            "cache_creation": int(lane.get("cache_creation_tokens") or 0),
            "cache_read": int(lane.get("cache_read_tokens") or 0),
        }

    if breakdown == "agent":
        series: list[dict[str, Any]] = []
        for client_entry in cube_30d["by_client"]:
            client = str(client_entry.get("client") or "")
            per_day: dict[str, dict[str, int]] = {}
            for entry in dated_periods:
                lane = (entry.get("by_client") or {}).get(client)
                if isinstance(lane, Mapping) and int(lane.get("total_tokens_including_cached") or 0) > 0:
                    per_day[str(entry.get("period"))] = lane_slot(lane)
            if per_day:
                series.append(
                    {
                        "label": _human_client(client),
                        "fill": f"var(--{client_lane_class(client)})",
                        "op": 1.0,
                        "per_day": per_day,
                    }
                )
        return line_days, bar_days, series

    # model / agent-model: re-aggregate the scoped records over the same 30-day
    # window (filter_usage_records is exactly what the cube used), honoring
    # usage_additive so held rows never enter a stack.
    kept, _unknown_time = filter_usage_records(
        scoped_records, record_time=_usage_record_time, days=30, today=today
    )
    by_key_day: dict[Any, dict[str, dict[str, int]]] = {}
    key_total: dict[Any, int] = {}
    key_client_tokens: dict[Any, dict[str, int]] = {}
    key_label: dict[Any, str] = {}
    for record in kept:
        if getattr(record, "usage_additive", True) is not True:
            continue
        day = usage_bucket_date(_usage_record_time(record))
        if day is None:
            continue
        day_iso = day.isoformat()
        client = str(getattr(record, "client", "") or "")
        model = str(getattr(record, "model", "") or "") or None
        if breakdown == "model":
            key: Any = model or "__unknown_model__"
            label = model or "Model unavailable"
        else:
            key = (client, model)
            label = f"{_human_client(client)} · {model or 'Model unavailable'}"
        key_label.setdefault(key, label)
        tokens = int(record.total_tokens_including_cached)
        slot = by_key_day.setdefault(key, {}).setdefault(
            day_iso,
            {"total": 0, "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0},
        )
        slot["total"] += tokens
        slot["input"] += int(record.input_tokens or 0)
        slot["output"] += int(record.output_tokens or 0)
        slot["cache_creation"] += int(record.cache_creation_input_tokens or 0)
        slot["cache_read"] += int(record.cache_read_input_tokens or 0)
        key_total[key] = key_total.get(key, 0) + tokens
        client_tokens = key_client_tokens.setdefault(key, {})
        client_tokens[client] = client_tokens.get(client, 0) + tokens

    ordered_keys = sorted(by_key_day, key=lambda key: (-key_total.get(key, 0), key_label.get(key, "")))
    head_keys = ordered_keys[:_OV_MAX_BREAKDOWN_SERIES]
    tail_keys = ordered_keys[_OV_MAX_BREAKDOWN_SERIES:]
    series = []
    client_rank: dict[str, int] = {}
    for key in head_keys:
        dominant_client = max(
            key_client_tokens[key].items(), key=lambda pair: (pair[1], pair[0])
        )[0]
        rank = client_rank.get(dominant_client, 0)
        client_rank[dominant_client] = rank + 1
        series.append(
            {
                "label": key_label[key],
                "fill": f"var(--{client_lane_class(dominant_client)})",
                "op": _ov_lane_opacity(rank),
                "per_day": by_key_day[key],
            }
        )
    if tail_keys:
        merged: dict[str, dict[str, int]] = {}
        for key in tail_keys:
            for day_iso, lane in by_key_day[key].items():
                slot = merged.setdefault(
                    day_iso, {"total": 0, "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
                )
                for field in ("total", "input", "output", "cache_creation", "cache_read"):
                    slot[field] += int(lane.get(field) or 0)
        series.append(
            {
                "label": f"Other models ({_fmt_int(len(tail_keys))})",
                "fill": "var(--lane-other)",
                "op": 1.0,
                "per_day": merged,
            }
        )
    return line_days, bar_days, series


def _cache_creation_cell_html(bucket: Mapping[str, Any], esc: Any) -> str:
    """Render cache writes without turning unsupported/unknown into zero."""

    excluded_rows = int(bucket.get("excluded_non_additive_rows") or 0)
    additive_rows = int(bucket.get("additive_rows") or 0)
    if excluded_rows and not additive_rows:
        return f'<span class="note">Unavailable · {esc(_fmt_int(excluded_rows))} row(s) held</span>'
    reporting = str(bucket.get("cache_creation_reporting") or "")
    if reporting == "not_reported":
        return '<span class="note">Not reported by this source</span>'
    if reporting == "unknown":
        return '<span class="note">Reporting capability unknown</span>'
    value = esc(_fmt_int(bucket.get("cache_creation_tokens")))
    if reporting == "partial":
        return f"<strong>{value}</strong><br><span class=\"note\">reported sources only</span>"
    return value


def _cache_read_cell_html(bucket: Mapping[str, Any], esc: Any) -> str:
    """Render cache reads without turning unsupported/unknown into zero."""

    excluded_rows = int(bucket.get("excluded_non_additive_rows") or 0)
    additive_rows = int(bucket.get("additive_rows") or 0)
    if excluded_rows and not additive_rows:
        return f'<span class="note">Unavailable · {esc(_fmt_int(excluded_rows))} row(s) held</span>'
    reporting = str(bucket.get("cache_read_reporting") or "")
    if reporting == "not_reported":
        return '<span class="note">Not reported by this source</span>'
    if reporting == "unknown":
        return '<span class="note">Reporting capability unknown</span>'
    value = esc(_fmt_int(bucket.get("cache_read_tokens")))
    if reporting == "partial":
        return f"<strong>{value}</strong><br><span class=\"note\">reported sources only</span>"
    return value


def _bucket_token_cell_html(bucket: Mapping[str, Any], key: str, esc: Any, *, strong: bool = False) -> str:
    """Render an additive token subtotal without turning held rows into zero."""

    excluded_rows = int(bucket.get("excluded_non_additive_rows") or 0)
    additive_rows = int(bucket.get("additive_rows") or 0)
    if excluded_rows and not additive_rows:
        return f'<span class="note">Unavailable · {esc(_fmt_int(excluded_rows))} row(s) held</span>'
    value = esc(_fmt_int(bucket.get(key)))
    rendered = f"<strong>{value}</strong>" if strong else value
    if excluded_rows:
        rendered += f'<br><span class="note">known subtotal · {esc(_fmt_int(excluded_rows))} row(s) held</span>'
    return rendered


def _tokens_by_platform_rows_html(cube: dict[str, Any], esc: Any) -> str:
    entries = [entry for entry in cube["by_client"] if isinstance(entry, dict)]
    max_total = max(
        (int(entry.get("total_tokens_including_cached") or 0) for entry in entries),
        default=0,
    )
    return "\n".join(
        "<tr>"
        f'<td><span class="badge-client {client_lane_class(entry.get("client"))}">{esc(_human_client(entry.get("client")))}</span></td>'
        f"<td>{_bucket_token_cell_html(entry, 'total_tokens_including_cached', esc, strong=True)}"
        f"{_total_bar_html(int(entry.get('total_tokens_including_cached') or 0), max_total, client_lane_class(entry.get('client')))}</td>"
        f"<td>{_bucket_token_cell_html(entry, 'input_tokens', esc)}</td>"
        f"<td>{_bucket_token_cell_html(entry, 'output_tokens', esc)}</td>"
        f"<td>{_cache_creation_cell_html(entry, esc)}</td>"
        f"<td>{_cache_read_cell_html(entry, esc)}</td>"
        f"<td>{_bucket_cost_cell_html(entry, esc)}</td>"
        f"<td>{esc(_fmt_int(entry.get('sessions')))}</td>"
        f"<td>{_client_models_cell(entry.get('models'), esc)}</td>"
        "</tr>"
        for entry in entries
    ) or '<tr><td colspan="9">No saved usage rows match this filter. Import usage (Refresh &amp; save usage) or widen the date range.</td></tr>'


def _stored_cost_coverage_label(entry: dict[str, Any]) -> str:
    """Stored-cost coverage: how many rows in the bucket carry a stored cost
    estimate vs how many carry none (the cost column's honest basis)."""

    costed = int(entry.get("priced_rows") or 0)
    uncosted = int(entry.get("unpriced_rows") or 0)
    if uncosted <= 0:
        return f"{_fmt_int(costed)} costed"
    if costed <= 0:
        return f"{_fmt_int(uncosted)} uncosted"
    return f"{_fmt_int(costed)} costed / {_fmt_int(uncosted)} uncosted"


def _bucket_cost_cell_html(entry: Mapping[str, Any], esc: Any) -> str:
    """Render stored cost with row coverage; a partial sum never looks whole."""

    rows = int((entry.get("additive_rows") if "additive_rows" in entry else entry.get("rows")) or 0)
    excluded_rows = int(entry.get("excluded_non_additive_rows") or 0)
    priced = int(entry.get("priced_rows") or 0)
    unpriced = int(entry.get("unpriced_rows") or 0)
    coverage = f"{_fmt_int(priced)}/{_fmt_int(rows)} rows priced" if rows else "no usage rows"
    cost = entry.get("estimated_cost_usd")
    if excluded_rows:
        known_cost = entry.get("known_additive_cost_usd")
        known = _fmt_optional_usd(known_cost) if known_cost is not None else _fmt_optional_usd(None)
        return (
            f'{known}<br><span class="note">known additive subtotal; complete cost unavailable · '
            f'{esc(_fmt_int(excluded_rows))} row(s) held · {esc(coverage)}</span>'
        )
    if cost is None:
        return f'{_fmt_optional_usd(None)}<br><span class="note">{esc(f"no cost estimate · {coverage}")}</span>'
    state = "partial estimate · " if unpriced else ""
    note = f"{state}{coverage} · {_bucket_confidence_label(dict(entry))}"
    return f'{_fmt_optional_usd(cost)}<br><span class="note">{esc(note)}</span>'


def _tokens_by_model_sort_key(cost_sort: str) -> Any:
    """Sort key over cube by-model buckets. ``total`` ranks by the STORED
    cost sum with complete buckets ahead of partial sums (rows without any
    stored cost sort last); the component sorts rank
    by their token counts (the per-component catalog costs died with the
    list-price recompute)."""

    def key(entry: dict[str, Any]) -> Any:
        if cost_sort == "agent":
            return (str(entry.get("client") or ""), str(entry.get("provider") or ""), str(entry.get("model") or ""))
        if cost_sort == "input":
            return int(entry.get("input_tokens") or 0)
        if cost_sort == "output":
            return int(entry.get("output_tokens") or 0)
        if cost_sort == "cache_create":
            return int(entry.get("cache_creation_tokens") or 0)
        if cost_sort == "cache_read":
            return int(entry.get("cache_read_tokens") or 0)
        if cost_sort == "tokens":
            return int(entry.get("total_tokens_including_cached") or 0)
        cost = entry.get("estimated_cost_usd")
        complete = cost is not None and int(entry.get("unpriced_rows") or 0) == 0
        return (complete, cost is not None, float(cost or 0.0))

    return key


def _tokens_by_model_table_parts(cube: dict[str, Any], esc: Any, cost_sort: str) -> tuple[str, str]:
    """(rows html, cap note) for the By model table (PRD §5.2.3): the cube's
    (client, provider, model) buckets — full token split plus the STORED-cost
    basis (sum of stored estimated_cost_usd with its confidence chip; no
    stored cost → the honest no-estimate dash). The same cube feeds By
    platform, By period, the filtered totals, and /usage/summary, so one
    page can never show two contradictory dollar figures for the same rows."""

    ordered = sorted(cube["by_model"], key=_tokens_by_model_sort_key(cost_sort), reverse=cost_sort != "agent")
    capped = capped_rows(ordered, 60)
    cap_note = _cap_note(capped["shown"], capped["total"], "model rows")
    rendered = []
    for entry in capped["rows"]:
        rendered.append(
            "<tr>"
            f"<td>{esc(_human_client(entry.get('client')))}</td>"
            f"<td>{esc(_display_provider_model(entry.get('provider'), entry.get('model')))}</td>"
            f"<td>{esc(_fmt_int(entry.get('rows')))}<br><span class=\"note\">{esc(_stored_cost_coverage_label(entry))}</span></td>"
            f"<td>{_bucket_token_cell_html(entry, 'total_tokens_including_cached', esc, strong=True)}</td>"
            f"<td>{_bucket_token_cell_html(entry, 'input_tokens', esc)}</td>"
            f"<td>{_bucket_token_cell_html(entry, 'output_tokens', esc)}</td>"
            f"<td>{_cache_creation_cell_html(entry, esc)}</td>"
            f"<td>{_cache_read_cell_html(entry, esc)}</td>"
            f"<td>{_bucket_cost_cell_html(entry, esc)}</td>"
            "</tr>"
        )
    rows_html = "\n".join(rendered) or (
        '<tr><td colspan="9">No saved usage rows match this filter. Import usage (Refresh &amp; save usage) or widen the date range.</td></tr>'
    )
    return rows_html, cap_note


def _tokens_by_period_table_parts(cube: dict[str, Any], esc: Any, granularity: str) -> tuple[str, str]:
    """(rows html, cap note) for the By period table: one row per day/week in
    range INCLUDING empty periods, newest first. The unknown-time bucket (if
    any) renders LAST and is pinned OUTSIDE the row cap — the filtered-totals
    line points readers at that row, so truncating it away would make the
    page assert a row that doesn't render."""

    dated = [entry for entry in cube["by_period"] if entry.get("period") != UNKNOWN_PERIOD]
    unknown = [entry for entry in cube["by_period"] if entry.get("period") == UNKNOWN_PERIOD]
    capped = capped_rows(list(reversed(dated)), _BY_PERIOD_TABLE_MAX)
    cap_note = _cap_note(capped["shown"], capped["total"], "periods")
    if cap_note and unknown:
        cap_note = cap_note.replace("</p>", " The Unknown time row below is pinned outside this cap.</p>")
    rows = "\n".join(
        "<tr>"
        f"<td>{esc(_chart_period_label(str(entry.get('period')), granularity))}</td>"
        f"<td>{_bucket_token_cell_html(entry, 'total_tokens_including_cached', esc, strong=True)}</td>"
        f"<td>{_bucket_token_cell_html(entry, 'input_tokens', esc)}</td>"
        f"<td>{_bucket_token_cell_html(entry, 'output_tokens', esc)}</td>"
        f"<td>{_cache_creation_cell_html(entry, esc)}</td>"
        f"<td>{_cache_read_cell_html(entry, esc)}</td>"
        f"<td>{_bucket_cost_cell_html(entry, esc)}</td>"
        f"<td>{esc(_fmt_int(entry.get('sessions')))}</td>"
        f"<td>{esc(_fmt_int(entry.get('rows')))}</td>"
        "</tr>"
        for entry in [*capped["rows"], *unknown]
    ) or '<tr><td colspan="9">No saved usage rows match this filter. Import usage (Refresh &amp; save usage) or widen the date range.</td></tr>'
    return rows, cap_note


# Data-driven filter pill rows (models on /tokens, projects on /sessions)
# are capped like every other data-driven list — a store accumulating
# distinct values over years must not turn the filter controls themselves
# into the page-bloat vector.
_FILTER_PILL_MAX = 12


def _capped_value_pills(values: list[str], *, current: str, param: str) -> tuple[list[str], str]:
    """(pill values, overflow note) for a data-driven pill row: the ranked
    top ``_FILTER_PILL_MAX`` values plus the currently selected one (an
    active filter must stay visible), with an honest note naming how many
    values did not get a pill."""

    shown = values[:_FILTER_PILL_MAX]
    if current != "all" and current in values and current not in shown:
        shown = [*shown[: _FILTER_PILL_MAX - 1], current]
    hidden = len(values) - len(shown)
    note = f' <span class="note">and {_fmt_int(hidden)} more (use ?{param}=)</span>' if hidden > 0 else ""
    return shown, note


def _tokens_filter_controls_html(
    *,
    client: str,
    model: str,
    url_model: str,
    days: str,
    granularity: str,
    effective_granularity: str,
    pill_models: list[str],
    model_pill_note: str,
    cost_sort: str,
    esc: Any,
) -> str:
    """Filter pill rows (PRD §5.2): every combination is a URL. Choices are
    whitelists (clients, day presets, granularity); model pills are the
    top total-token models in the current range (capped, with an honest
    overflow note). ``url_model`` is the value safe to re-encode into pill
    URLs — an unknown ``model`` renders the empty result with the filter
    named ONCE in the note, never echoed into every constructed URL."""

    params = {"client": client, "model": url_model, "days": days, "granularity": granularity, "cost_sort": cost_sort}
    client_control = _sort_control(
        current=client,
        options=[("all", "All platforms"), *[(name, _human_client(name)) for name in KNOWN_USAGE_CLIENTS]],
        param="client",
        extra=params,
        esc=esc,
        base="/tokens",
    )
    model_control = _sort_control(
        current=model,
        options=[("all", "All models"), *[(name, name) for name in pill_models]],
        param="model",
        extra=params,
        esc=esc,
        base="/tokens",
    ) + model_pill_note
    days_control = _sort_control(
        current=days,
        options=[("7", "7 days"), ("30", "30 days"), ("90", "90 days"), ("all", "All time")],
        param="days",
        extra=params,
        esc=esc,
        base="/tokens",
    )
    granularity_control = _sort_control(
        current=granularity,
        options=[("auto", "Auto"), ("daily", "Daily"), ("weekly", "Weekly")],
        param="granularity",
        extra=params,
        esc=esc,
        base="/tokens",
    )
    return (
        '<div class="filter-rows">'
        f'<div class="filter-row"><span class="filter-label">Platform</span>{client_control}</div>'
        f'<div class="filter-row"><span class="filter-label">Model</span>{model_control}</div>'
        f'<div class="filter-row"><span class="filter-label">Date range</span>{days_control}</div>'
        f'<div class="filter-row"><span class="filter-label">Granularity</span>{granularity_control} '
        f'<span class="note">auto = daily for 7/30-day ranges, weekly for 90/all (now: {esc(effective_granularity)})</span></div>'
        "</div>"
    )


def _tokens_filtered_totals_html(cube: dict[str, Any], *, days: str, esc: Any) -> str:
    """Filtered totals restate their basis (PRD §4 honesty rule for filters):
    cache-inclusive total and estimated cost lead; token categories remain an
    explicit breakdown, with capability language for cache writes."""

    totals = cube["totals"]
    unknown_rows = int(totals.get("unknown_time_rows") or 0)
    if not unknown_rows:
        unknown_note = ""
    elif days == "all":
        unknown_note = f" {_fmt_int(unknown_rows)} row(s) without a usable timestamp appear under Unknown time."
    else:
        unknown_note = f" {_fmt_int(unknown_rows)} row(s) without a usable timestamp are excluded from date-filtered views."
    cache_write_reporting = str(totals.get("cache_creation_reporting") or "")
    cache_read_reporting = str(totals.get("cache_read_reporting") or "")
    cache_split_note = (
        " Cache split is partial; totals still include unseparated input."
        if cache_write_reporting in {"not_reported", "partial", "unknown"}
        or cache_read_reporting in {"not_reported", "partial", "unknown"}
        else ""
    )
    rows = int(totals.get("rows") or 0)
    additive_rows = int(totals.get("additive_rows") or 0)
    excluded_rows = int(totals.get("excluded_non_additive_rows") or 0)
    if rows == 0:
        return (
            '<p class="section-note" id="tokens-filtered-totals"><strong>Usage unavailable for this filter.</strong> '
            "No imported usage rows match this filter; token and cost totals are unknown, not zero.</p>"
        )
    if excluded_rows and not additive_rows:
        return (
            '<p class="section-note" id="tokens-filtered-totals"><strong>Usage unavailable for this filter.</strong> '
            f'{esc(_fmt_int(excluded_rows))} usage '
            f'{"row is" if excluded_rows == 1 else "rows are"} held until source identity or lineage normalization; '
            'raw cumulative counters remain available in the ledger and are not presented as zero usage or cost.</p>'
        )
    priced_rows = int(totals.get("priced_rows") or 0)
    unpriced_rows = int(totals.get("unpriced_rows") or 0)
    cost_coverage = f"{_fmt_int(priced_rows)}/{_fmt_int(additive_rows)} additive rows priced"
    if excluded_rows:
        known_cost = totals.get("known_additive_cost_usd")
        cost_fragment = (
            f"complete cost unavailable; {_fmt_optional_usd_text(known_cost)} known additive subtotal "
            f"({cost_coverage})"
            if known_cost is not None
            else f"complete cost unavailable ({cost_coverage})"
        )
    elif totals.get("estimated_cost_usd") is None:
        # Without a cost figure the generic template garbled into "No
        # estimate estimated, not a provider bill (no cost estimate)" — say
        # it once.
        cost_fragment = f"no cost estimate ({cost_coverage})"
    else:
        estimate_label = "partial estimate" if unpriced_rows else "estimated"
        cost_fragment = (
            f"{_fmt_optional_usd_text(totals.get('estimated_cost_usd'))} {estimate_label}, not a provider bill "
            f"({_bucket_confidence_label(totals)}; {cost_coverage})"
        )
    if cache_write_reporting == "not_reported":
        cache_write_fragment = "cache writes not reported by this source"
    elif cache_write_reporting == "unknown":
        cache_write_fragment = "cache-write reporting capability unknown"
    elif cache_write_reporting == "partial":
        cache_write_fragment = (
            f"{_fmt_int(totals.get('cache_creation_tokens'))} reported cache writes "
            "(partial source coverage)"
        )
    else:
        cache_write_fragment = f"{_fmt_int(totals.get('cache_creation_tokens'))} cache writes"
    if cache_read_reporting == "not_reported":
        cache_read_fragment = "cache reads not reported by this source"
    elif cache_read_reporting == "unknown":
        cache_read_fragment = "cache-read reporting capability unknown"
    elif cache_read_reporting == "partial":
        cache_read_fragment = f"{_fmt_int(totals.get('cache_read_tokens'))} reported cache reads (partial source coverage)"
    else:
        cache_read_fragment = f"{_fmt_int(totals.get('cache_read_tokens'))} cache reads"
    total_label = "known additive subtotal" if excluded_rows else "total tokens"
    held_fragment = (
        f" {_fmt_int(excluded_rows)} additional row(s) held for source identity or lineage normalization."
        if excluded_rows
        else ""
    )
    return (
        '<p class="section-note" id="tokens-filtered-totals">Filtered totals: '
        f"<strong>{esc(_fmt_int(totals.get('total_tokens_including_cached')))} {esc(total_label)}</strong> (incl. caches) · "
        f"{esc(cost_fragment)} · "
        f"{esc(_fmt_int(totals.get('sessions')))} session(s) · {esc(_fmt_int(totals.get('rows')))} usage row(s)."
        f" Breakdown: {esc(_fmt_int(totals.get('input_tokens')))} input after reported cache · "
        f"{esc(_fmt_int(totals.get('output_tokens')))} output · {esc(cache_write_fragment)} · "
        f"{esc(cache_read_fragment)}."
        f"{esc(cache_split_note)}{esc(held_fragment)}{esc(unknown_note)}</p>"
    )


def _tokens_preserved_history_html(
    *,
    current_cube: Mapping[str, Any],
    all_time_cube: Mapping[str, Any] | None,
    saved_records: list[DashboardUsageRecord],
    client: str,
    model: str,
    days: str,
    granularity: str,
    cost_sort: str,
    today: date,
    esc: Any,
) -> str:
    """Explain clients that disappear only because of the date window."""

    if days == "all" or not isinstance(all_time_cube, Mapping):
        return ""
    history_outside_range = _usage_history_outside_range(
        current_cube=current_cube,
        all_time_cube=all_time_cube,
        records=saved_records,
        model=None if model == "all" else model,
        days=days_choice_to_int(days),
        today=today,
    )
    preserved: list[str] = []
    for row in history_outside_range:
        client_name = str(row["client"])
        rows = int(row["rows"])
        latest = _fmt_time(row["latest_activity_at"])
        detail = f"{_human_client(client_name)} — {_fmt_int(rows)} all-time row(s)"
        if latest:
            detail += f", latest {latest}"
        preserved.append(detail)
    if not preserved:
        return ""
    all_time_url = "/tokens?" + urlencode(
        {
            "client": client,
            "model": model,
            "days": "all",
            "granularity": granularity,
            "cost_sort": cost_sort,
        }
    )
    return (
        '<p class="section-note range-preservation-note"><strong>Saved history is preserved.</strong> '
        f'No rows in this window for {esc("; ".join(preserved))}. '
        f'<a href="{esc(all_time_url)}">View all time</a>.</p>'
    )


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


def _latest_machine_check_events(item: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], float]]:
    """Return the newest event for each stable check identity.

    The ledger's conservative evidence status preserves every historical
    failure. The product state asks a different question: did a failure arrive
    after the latest work report, or was it followed by newer check/work state?
    """

    evidence_events = (
        item.get("current_check_events")
        if isinstance(item.get("current_check_events"), list)
        else item.get("evidence_events")
    )
    if not isinstance(evidence_events, list):
        return []
    latest: dict[str, tuple[float, int, int, Mapping[str, Any]]] = {}
    for index, event in enumerate(evidence_events):
        if not isinstance(event, Mapping):
            continue
        result = str(event.get("result") or "unknown").lower()
        if result not in {"passed", "failed", "error"}:
            continue
        identity = finding_check_key(event, task_scoped=True)
        observed = float(event.get("created_at") or event.get("time") or 0.0)
        arrival_sequence = _safe_int(event.get("arrival_sequence"))
        candidate = (observed, arrival_sequence, index, event)
        if identity not in latest or candidate[:3] > latest[identity][:3]:
            latest[identity] = candidate
    return [(value[3], value[0]) for _identity, value in sorted(latest.items())]


def _latest_machine_check_results(item: Mapping[str, Any]) -> list[tuple[str, float]]:
    """Return ``(result, observed_at)`` for each stable check identity."""

    return [
        (str(event.get("result") or "unknown").lower(), observed_at)
        for event, observed_at in _latest_machine_check_events(item)
    ]


def _latest_actionable_failed_check(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the newest check failure without pretending newer generic work fixed it."""

    episode_states = {
        str(episode.get("target_digest")): str(episode.get("disposition_state") or "open")
        for episode in (
            item.get("finding_episodes")
            if isinstance(item.get("finding_episodes"), list)
            else []
        )
        if isinstance(episode, Mapping) and episode.get("target_digest")
    }
    failures = [
        (observed_at, event)
        for event, observed_at in _latest_machine_check_events(item)
        if str(event.get("result") or "unknown").lower() in {"failed", "error"}
        and episode_states.get(str(finding_target_digest(event) or ""), "open") == "open"
    ]
    return max(failures, key=lambda entry: entry[0])[1] if failures else None


def _work_product_state(item: Mapping[str, Any]) -> dict[str, Any]:
    """Translate ledger detail into one user-facing state.

    Integration hygiene is intentionally absent here. Missing join keys,
    context coverage, and unattributed usage remain inspectable in Sessions
    and Advanced, but they are not work that the user must personally fix.
    """

    status = str(item.get("latest_status") or "unknown").lower()
    evidence = str(item.get("evidence_status") or "none").lower()
    blocker_resolution = (
        item.get("blocker_resolution")
        if isinstance(item.get("blocker_resolution"), Mapping)
        else {}
    )
    latest_check_events = _latest_machine_check_events(item)
    latest_checks = [
        (str(event.get("result") or "unknown").lower(), observed_at)
        for event, observed_at in latest_check_events
    ]
    if status == "resolved" and blocker_resolution.get("state") == "resolved":
        return {
            "key": "resolved",
            "label": "Resolved",
            "css": "status-missing",
            "action_required": False,
            "rank": 3,
            "why": (
                "A later passed check explicitly resolved the exact blocker episode. "
                "This is an evidence-backed agent report, not a fabricated completed or verified outcome."
            ),
        }
    if status in {"blocked", "failed"} or str(item.get("blocker") or "").strip():
        return {
            "key": "blocked",
            "label": "Blocked",
            "css": "status-error",
            "action_required": True,
            "rank": 0,
            "why": "The agent reported that this work cannot continue without input or a dependency change.",
        }
    # A failed check is evidence about the work, not evidence that agentacct
    # itself failed and not proof that the dashboard user owns the next action.
    # Keep the finding visible until the same source-scoped stable check
    # passes (or a later explicit disposition says otherwise); generic newer
    # work is not proof that the finding went away.
    failed_events = [
        event
        for event, _observed_at in latest_check_events
        if str(event.get("result") or "unknown").lower() in {"failed", "error"}
    ]
    if failed_events:
        episode_states = {
            str(episode.get("target_digest")): str(episode.get("disposition_state") or "open")
            for episode in (
                item.get("finding_episodes")
                if isinstance(item.get("finding_episodes"), list)
                else []
            )
            if isinstance(episode, Mapping) and episode.get("target_digest")
        }
        finding_states = [
            episode_states.get(str(finding_target_digest(event) or ""), "open")
            for event in failed_events
        ]
        if "open" not in finding_states:
            reviewed = "reviewed" in finding_states
            return {
                "key": "finding_reviewed" if reviewed else "finding_resolved",
                "label": "Finding reviewed" if reviewed else "Marked resolved",
                "css": "status-missing",
                "action_required": False,
                "finding_open": False,
                "finding_present": True,
                "rank": 3,
                "why": (
                    "Every current finding was reviewed or marked resolved; no passing check has replaced the failed evidence."
                    if reviewed
                    else "The dashboard user marked this finding resolved; this is not machine verification."
                ),
            }
        return {
            "key": "open_finding",
            "label": "Open finding",
            "css": "status-finding",
            "action_required": False,
            "finding_open": True,
            "rank": 2,
            "why": (
                "An agent-reported check found an issue in the work. agentacct preserves that evidence; "
                "it is not a agentacct system failure or an inferred user assignment."
            ),
        }
    if status in {"started", "checkpoint", "active", "in_progress"}:
        return {
            "key": "in_progress",
            "label": "In progress",
            "css": "status-needs-import",
            "action_required": False,
            "rank": 1,
            "why": "The latest work report says this task is still in progress.",
        }
    latest_checks_all_pass = bool(latest_checks) and all(result == "passed" for result, _ in latest_checks)
    projected_checks = isinstance(item.get("current_check_events"), list)
    if status in {"completed", "passed"} and (
        latest_checks_all_pass
        or (evidence == "strong" and not latest_checks and not projected_checks)
    ):
        return {
            "key": "verified",
            "label": "Verified",
            "css": "status-found",
            "action_required": False,
            "rank": 2,
            "why": "The completion report has a linked passing machine check.",
        }
    if status in {"completed", "passed"}:
        return {
            "key": "reported_done",
            "label": "Agent reported",
            "css": "status-missing",
            "action_required": False,
            "rank": 3,
            "why": "The agent reported completion. agentacct does not have a fully passing latest check set linked to this result.",
        }
    return {
        "key": "reported",
        "label": "Reported",
        "css": "status-missing",
        "action_required": False,
        "rank": 3,
        "why": "This state comes from the latest agent work report.",
    }


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


def _agent_mark(client: Any) -> str:
    """A tiny text mark for the product UI; no remote logo assets required."""

    return {
        "claude-code": "CC",
        "codex": "CX",
        "hermes": "HM",
        "opencode": "OC",
        "openclaw": "CL",
        "cursor": "CR",
    }.get(str(client or "").strip(), "AI")


def _session_model_labels(entry: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(entry, Mapping):
        return []
    usage = entry.get("usage") if isinstance(entry.get("usage"), Mapping) else {}
    lanes = usage.get("model_lanes") if isinstance(usage.get("model_lanes"), list) else []
    labels: list[str] = []
    observed = entry.get("observed_models") if isinstance(entry.get("observed_models"), list) else []
    identity_models = usage.get("identity_models") if isinstance(usage.get("identity_models"), list) else []
    for model in [*observed, *identity_models]:
        label = str(model or "").strip()
        if label and label not in labels:
            labels.append(label)
    for lane in lanes:
        if not isinstance(lane, Mapping):
            continue
        label = str(lane.get("model") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _session_identity_key(entry: Mapping[str, Any]) -> tuple[str, str]:
    return (str(entry.get("client") or ""), str(entry.get("client_session_id") or ""))


def _session_parent_identity_key(entry: Mapping[str, Any]) -> tuple[str, str] | None:
    related = entry.get("related") if isinstance(entry.get("related"), Mapping) else {}
    parent = related.get("parent") if isinstance(related.get("parent"), Mapping) else None
    if not parent or parent.get("client_session_id") is None:
        return None
    return (str(entry.get("client") or ""), str(parent.get("client_session_id") or ""))


def _aggregate_session_usage(sessions: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum each rollup entry's own usage exactly once.

    ``related.children_usage`` is intentionally ignored because it is already
    a descendant subtotal. Adding it here would double count child/internal
    sessions when a root run is collapsed for the product homepage.
    """

    totals = {
        "rows": 0,
        "additive_rows": 0,
        "excluded_non_additive_rows": 0,
        "priced_rows": 0,
        "unpriced_rows": 0,
        "fresh_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    }
    models: list[str] = []
    priced_cost = 0.0
    cost_is_complete = True
    usage_rows_present = False
    for entry in sessions:
        usage = entry.get("usage") if isinstance(entry.get("usage"), Mapping) else {}
        rows = int(usage.get("rows") or 0)
        additive_rows = int((usage.get("additive_rows") if "additive_rows" in usage else rows) or 0)
        excluded_non_additive_rows = int(usage.get("excluded_non_additive_rows") or 0)
        if "priced_rows" in usage or "unpriced_rows" in usage:
            priced_rows = int(usage.get("priced_rows") or 0)
            unpriced_rows = int(usage.get("unpriced_rows") or 0)
        elif rows and usage.get("estimated_cost_usd") is not None:
            priced_rows, unpriced_rows = rows, 0
        else:
            priced_rows, unpriced_rows = 0, rows
        totals["rows"] += rows
        totals["additive_rows"] += additive_rows
        totals["excluded_non_additive_rows"] += excluded_non_additive_rows
        totals["priced_rows"] += priced_rows
        totals["unpriced_rows"] += unpriced_rows
        totals["fresh_tokens"] += int(usage.get("fresh_tokens") or 0)
        totals["cache_creation_tokens"] += int(usage.get("cache_creation_tokens") or 0)
        totals["cache_read_tokens"] += int(usage.get("cache_read_tokens") or 0)
        totals["total_tokens"] += int(usage.get("total_tokens") or 0)
        if additive_rows:
            usage_rows_present = True
            if unpriced_rows or usage.get("estimated_cost_usd") is None:
                cost_is_complete = False
            else:
                priced_cost += float(usage.get("estimated_cost_usd") or 0.0)
        for model in _session_model_labels(entry):
            if model not in models:
                models.append(model)
    if totals["excluded_non_additive_rows"]:
        cost_is_complete = False
    totals["estimated_cost_usd"] = priced_cost if usage_rows_present and cost_is_complete else None
    totals["known_additive_cost_usd"] = priced_cost if usage_rows_present and priced_cost else None
    totals["cost_complete"] = bool(usage_rows_present and cost_is_complete)
    totals["usage_availability"] = (
        "partial"
        if totals["additive_rows"] and totals["excluded_non_additive_rows"]
        else "held"
        if totals["excluded_non_additive_rows"]
        else "available"
        if totals["additive_rows"]
        else "unknown"
    )
    totals["model_lanes"] = [{"model": model} for model in models]
    return totals


def _overview_root_session_groups(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse every resolvable descendant onto its transitive root.

    The Sessions explorer deliberately preserves a one-level forensic view.
    Work needs a different projection: root runs are the product unit and
    child/auto-review sessions are supporting activity. Parent chains are
    followed transitively; missing parents become honest orphan roots and
    cycles collapse deterministically instead of hiding any session.
    """

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        key = _session_identity_key(entry)
        if not key[0] or not key[1] or key in by_key:
            continue
        by_key[key] = entry
        order.append(key)

    parent_of = {key: _session_parent_identity_key(entry) for key, entry in by_key.items()}
    resolved: dict[tuple[str, str], tuple[str, str]] = {}
    resolved_state: dict[tuple[str, str], str] = {}
    for start in order:
        if start in resolved:
            continue
        path: list[tuple[str, str]] = []
        position: dict[tuple[str, str], int] = {}
        current = start
        while True:
            if current in resolved:
                root_key = resolved[current]
                lineage_state = resolved_state[current]
                break
            if current in position:
                cycle = path[position[current] :]
                root_key = min(cycle)
                lineage_state = "cycle"
                break
            position[current] = len(path)
            path.append(current)
            parent = parent_of.get(current)
            if parent is None:
                root_key = current
                lineage_state = (
                    "resolved_root"
                    if str(by_key[current].get("session_kind") or "root") == "root"
                    else "orphan_parent_missing"
                )
                break
            if parent not in by_key:
                root_key = current
                lineage_state = "orphan_parent_missing"
                break
            current = parent
        for member in path:
            resolved[member] = root_key
            resolved_state[member] = lineage_state

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key in order:
        grouped.setdefault(resolved[key], []).append(by_key[key])

    groups: list[dict[str, Any]] = []
    for root_key, members in grouped.items():
        root = by_key[root_key]
        members = sorted(
            members,
            key=lambda entry: (
                0 if _session_identity_key(entry) == root_key else 1,
                float(entry.get("last_activity_at") or 0.0),
            ),
        )
        supporting_members = [entry for entry in members if _session_identity_key(entry) != root_key]
        child_count = sum(
            1 for entry in supporting_members if str(entry.get("session_kind") or "root") == "child"
        )
        internal_count = sum(
            1 for entry in supporting_members if str(entry.get("session_kind") or "root") == "internal"
        )
        models: list[str] = []
        work_titles: list[str] = []
        for entry in members:
            for model in _session_model_labels(entry):
                if model not in models:
                    models.append(model)
            work = entry.get("work") if isinstance(entry.get("work"), Mapping) else {}
            work_items = work.get("items") if isinstance(work.get("items"), list) else []
            for item in work_items:
                if not isinstance(item, Mapping):
                    continue
                title = str(item.get("title") or item.get("section_id") or "").strip()
                if title and title not in work_titles:
                    work_titles.append(title)
        groups.append(
            {
                "root": root,
                "members": members,
                "member_keys": {_session_identity_key(entry) for entry in members},
                "client": root.get("client"),
                "project": root.get("project"),
                "models": models,
                "work_titles": work_titles,
                "usage": _aggregate_session_usage(members),
                "supporting_count": len(supporting_members),
                "child_count": child_count,
                "internal_count": internal_count,
                "lineage_state": resolved_state.get(root_key, "resolved_root"),
                "last_activity_at": max(float(entry.get("last_activity_at") or 0.0) for entry in members),
            }
        )
    return groups


def _continuation_membership_rows(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Adapt the append-only continuation projection to the pure Task reducer."""

    tasks = projection.get("tasks") if isinstance(projection.get("tasks"), list) else []
    rows: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        sessions: list[dict[str, Any]] = []
        memberships = task.get("memberships") if isinstance(task.get("memberships"), list) else []
        for membership in memberships:
            if not isinstance(membership, Mapping):
                continue
            session = membership.get("session") if isinstance(membership.get("session"), Mapping) else {}
            client = str(session.get("client") or "").strip()
            session_id = str(session.get("client_session_id") or "").strip()
            if not client or not session_id:
                continue
            sessions.append(
                {
                    "client": client,
                    "client_session_id": session_id,
                    "primary": str(membership.get("role") or "") == "primary",
                }
            )
        task_id = str(task.get("task_id") or "").strip()
        if task_id and sessions:
            rows.append(
                {
                    "task_id": task_id,
                    "title_override": task.get("title"),
                    "sessions": sessions,
                }
            )
    return rows


def _overview_residual_session_group(
    group: Mapping[str, Any], consumed_keys: set[tuple[str, str]]
) -> dict[str, Any] | None:
    """Keep one collapsed activity card for lineage members not represented by work cards."""

    original_members = group.get("members") if isinstance(group.get("members"), list) else []
    remaining = [entry for entry in original_members if _session_identity_key(entry) not in consumed_keys]
    if not remaining:
        return None
    if len(remaining) == len(original_members):
        return dict(group)

    original_root = group.get("root") if isinstance(group.get("root"), Mapping) else {}
    root_key = _session_identity_key(original_root)
    root_remains = root_key not in consumed_keys
    display_root = original_root if root_remains else remaining[0]
    supporting_members = [entry for entry in remaining if _session_identity_key(entry) != root_key]
    if not root_remains:
        supporting_members = remaining
    models: list[str] = []
    work_titles: list[str] = []
    for entry in remaining:
        for model in _session_model_labels(entry):
            if model not in models:
                models.append(model)
        work = entry.get("work") if isinstance(entry.get("work"), Mapping) else {}
        work_items = work.get("items") if isinstance(work.get("items"), list) else []
        for item in work_items:
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or item.get("section_id") or "").strip()
            if title and title not in work_titles:
                work_titles.append(title)
    return {
        "root": display_root,
        "members": remaining,
        "member_keys": {_session_identity_key(entry) for entry in remaining},
        "client": original_root.get("client") or display_root.get("client"),
        "project": original_root.get("project") or display_root.get("project"),
        "models": models,
        "work_titles": work_titles,
        "usage": _aggregate_session_usage(remaining),
        "supporting_count": len(supporting_members),
        "child_count": sum(
            1 for entry in supporting_members if str(entry.get("session_kind") or "root") == "child"
        ),
        "internal_count": sum(
            1 for entry in supporting_members if str(entry.get("session_kind") or "root") == "internal"
        ),
        "lineage_state": (
            "residual_root" if root_remains else "residual_supporting"
        ),
        "last_activity_at": max(float(entry.get("last_activity_at") or 0.0) for entry in remaining),
    }


def _model_chips_html(
    models: list[str], esc: Any, *, limit: int = 2, unknown_label: str | None = None
) -> str:
    if not models:
        if unknown_label:
            return f'<span class="model-chip is-unknown">{esc(unknown_label)}</span>'
        return ""
    visible = models[:limit]
    chips = [f'<span class="model-chip">{esc(model)}</span>' for model in visible]
    if len(models) > len(visible):
        chips.append(f'<span class="model-chip">+{esc(_fmt_int(len(models) - len(visible)))}</span>')
    return "".join(chips)


def _session_run_metrics(entry: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    if not isinstance(entry, Mapping):
        return []
    metrics: list[tuple[str, str]] = []
    duration = _fmt_duration_hm(entry.get("duration_seconds"))
    if duration:
        metrics.append((duration, "duration"))
    usage = entry.get("usage") if isinstance(entry.get("usage"), Mapping) else {}
    turns = usage.get("turns_total")
    if turns is not None:
        metrics.append((f"{_fmt_int(turns)} turns", "conversation"))
    additive_rows = int((usage.get("additive_rows") if "additive_rows" in usage else usage.get("rows")) or 0)
    excluded_rows = int(usage.get("excluded_non_additive_rows") or 0)
    if additive_rows > 0:
        token_label = "known subtotal tokens" if excluded_rows else "total tokens"
        metrics.append((f"{_fmt_compact_int(usage.get('total_tokens'))} {token_label}", "all reported categories"))
        if usage.get("estimated_cost_usd") is not None:
            metrics.append((f"{_fmt_optional_usd_text(usage.get('estimated_cost_usd'))} est.", "cost"))
    if excluded_rows:
        metrics.append((f"{_fmt_int(excluded_rows)} usage row{'s' if excluded_rows != 1 else ''} held", "awaiting source identity or lineage normalization"))
    related = entry.get("related") if isinstance(entry.get("related"), Mapping) else {}
    child_count = int(related.get("child_session_count") or 0)
    if child_count:
        metrics.append((f"{_fmt_int(child_count)} subagent{'s' if child_count != 1 else ''}", "parallel work"))
    return metrics


def _work_item_run_metrics(
    item: Mapping[str, Any], session: Mapping[str, Any] | None
) -> list[tuple[str, str]]:
    """Work-card metrics with session and attributed scopes made explicit."""

    metrics: list[tuple[str, str]] = []
    if isinstance(session, Mapping):
        duration = _fmt_duration_hm(session.get("duration_seconds"))
        if duration:
            metrics.append((duration, "session duration"))
        usage = session.get("usage") if isinstance(session.get("usage"), Mapping) else {}
        turns = usage.get("turns_total")
        if turns is not None:
            metrics.append((f"{_fmt_int(turns)} turns", "session total"))
    if int(item.get("linked_usage_records") or 0) > 0:
        metrics.append((f"{_fmt_compact_int(item.get('usage_total'))} total tokens", "attributed usage"))
        confidence = (
            item.get("cost_confidence_breakdown")
            if isinstance(item.get("cost_confidence_breakdown"), Mapping)
            else {}
        )
        has_cost_basis = any(
            int(count or 0) > 0 and str(basis) not in {"unknown", "subscription_unavailable"}
            for basis, count in confidence.items()
        )
        if has_cost_basis:
            metrics.append((f"{_fmt_optional_usd_text(item.get('estimated_cost_total'))} est.", "attributed cost"))
    if isinstance(session, Mapping):
        related = session.get("related") if isinstance(session.get("related"), Mapping) else {}
        child_count = int(related.get("child_session_count") or 0)
        if child_count:
            metrics.append((f"{_fmt_int(child_count)} subagent{'s' if child_count != 1 else ''}", "session lineage"))
    return metrics


def _run_metrics_html(metrics: list[tuple[str, str]], esc: Any) -> str:
    if not metrics:
        return ""
    # Four signals keep a card glanceable. The full usage/session breakdown
    # remains one click away in All activity and Usage.
    cells = "".join(
        f'<div class="run-metric"><strong>{esc(value)}</strong><span>{esc(label)}</span></div>'
        for value, label in metrics[:4]
    )
    return f'<div class="run-metrics">{cells}</div>'


def _run_flow_html(*, state_key: str, has_work: bool, esc: Any) -> str:
    """Render a semantic four-signal run path from saved evidence.

    This is deliberately not a fabricated tool trace. It shows which product
    stages agentacct can support from saved session/work/check/outcome data.
    """

    if not has_work:
        classes = ("is-done", "", "", "")
        description = "Activity observed; task, check, and outcome were not recorded."
    elif state_key == "verified":
        classes = ("is-done", "is-done", "is-done", "is-done")
        description = "Activity and task recorded; passing check linked; outcome verified."
    elif state_key == "reported_done":
        classes = ("is-done", "is-done", "", "is-reported")
        description = "Activity and task recorded; outcome reported; no passing check is linked."
    elif state_key == "resolved":
        classes = ("is-done", "is-done", "is-done", "is-reported")
        description = "Activity and blocker recorded; a later passing check explicitly reports that blocker resolved."
    elif state_key == "open_finding":
        classes = ("is-done", "is-done", "is-finding", "")
        description = "Activity and task recorded; an agent-reported finding remains open."
    elif state_key in {"finding_reviewed", "finding_resolved"}:
        classes = ("is-done", "is-done", "is-reported", "")
        description = (
            "Activity and task recorded; the failed check remains objective evidence, "
            "and its attention state was reviewed without creating a verified outcome."
        )
    elif state_key == "blocked":
        classes = ("is-done", "is-warning", "", "is-warning")
        description = "Activity recorded; task is blocked before a verified outcome."
    elif state_key == "in_progress":
        classes = ("is-done", "is-active", "", "")
        description = "Activity recorded; task remains in progress."
    else:
        classes = ("is-done", "is-done", "", "")
        description = "Activity and task report recorded; check and outcome are not established."
    labels = ("Observed", "Task", "Check", "Outcome")
    steps = "".join(
        f'<span class="run-step {css}">{label}</span>' for css, label in zip(classes, labels, strict=True)
    )
    return f'<div class="run-flow" role="img" aria-label="{esc(description)}">{steps}</div>'


def _run_card_html(
    *,
    title: str,
    client: Any,
    meta: list[str],
    state: Mapping[str, Any],
    summary: str,
    status_basis: str,
    metrics: list[tuple[str, str]],
    esc: Any,
    session: Mapping[str, Any] | None = None,
    models: list[str] | None = None,
    session_line: str | None = None,
    action_html: str = "",
    details_label: str = "Run details",
    details_extra_html: str = "",
    sessions_html: str = "",
    has_work: bool,
    title_href: str | None = None,
) -> str:
    session = session if isinstance(session, Mapping) else {}
    display_client = session.get("client") or client
    lane_class = client_lane_class(display_client)
    visible_models = list(models) if models is not None else _session_model_labels(session)
    if session_line is None:
        session_bits: list[str] = []
        if session.get("session_kind"):
            session_bits.append(f"{_record_kind_label(session.get('session_kind'))} session")
        elif session:
            session_bits.append("Saved session")
        session_line = " · ".join(session_bits) if session_bits else "Recorded work"
    if state.get("action_required"):
        card_class = " action-required"
    elif state.get("finding_open"):
        card_class = " finding-open"
    else:
        card_class = ""
    state_key = str(state.get("key") or "reported")
    details_summary = summary or "No additional run summary was recorded."
    title_html = (
        f'<a class="task-title-link" href="{esc(title_href)}">{esc(title)}</a>'
        if title_href
        else esc(title)
    )
    return f"""
    <article class="work-feed-item{card_class}">
      <div class="run-card-shell state-{esc(state_key)}">
        <span class="run-card-accent {esc(lane_class)}" aria-hidden="true"></span>
        <div class="run-card-header">
          <div class="run-identity">
            <span class="agent-avatar {esc(lane_class)}" aria-hidden="true">{esc(_agent_mark(display_client))}</span>
            <div class="run-identity-copy">
              <div class="run-agent-line"><strong>{esc(_human_client(display_client))}</strong>{_model_chips_html(visible_models, esc)}</div>
              <div class="run-session-line">{esc(session_line)}</div>
            </div>
          </div>
          <span class="status {esc(state.get('css'))}">{esc(state.get('label'))}</span>
        </div>
        <h3 class="work-feed-title">{title_html}</h3>
        <div class="work-feed-meta">{esc(' · '.join(meta))}</div>
        {_run_flow_html(state_key=state_key, has_work=has_work, esc=esc)}
        {action_html}
        {_run_metrics_html(metrics, esc)}
        {sessions_html}
        <details class="work-feed-why"><summary>{esc(details_label)}</summary><p>{esc(details_summary)}</p>{details_extra_html}<p><strong>Status basis:</strong> {esc(status_basis)}</p></details>
      </div>
    </article>
    """


def _work_action_html(item: Mapping[str, Any], state: Mapping[str, Any], esc: Any) -> str:
    action_parts: list[str] = []
    blocker = str(item.get("blocker") or "").strip()
    next_step = str(item.get("next_step") or "").strip()
    state_key = str(state.get("key") or "")
    resolution = (
        item.get("blocker_resolution")
        if isinstance(item.get("blocker_resolution"), Mapping)
        else {}
    )
    if state_key == "resolved":
        summary = str(resolution.get("summary") or "").strip() or (
            "A later passed check explicitly reported this blocker resolved."
        )
        return (
            '<div class="work-feed-action"><strong>Resolution reported</strong>'
            f"<p>{esc(summary)}</p>"
            '<p class="work-feed-finding-context">The original blocker stays in history. '
            "agentacct does not relabel this as a verified completion.</p></div>"
        )
    if blocker and state_key in {"blocked", "in_progress"}:
        action_parts.append(f"<strong>What is blocked</strong><p>{esc(blocker)}</p>")
        if resolution.get("state") == "partially_resolved":
            partial_summary = str(resolution.get("summary") or "").strip()
            if partial_summary:
                action_parts.append(
                    f"<strong>Partial resolution reported</strong><p>{esc(partial_summary)}</p>"
                )
    if state_key == "open_finding":
        failed_check = _latest_actionable_failed_check(item)
        finding = failed_check if isinstance(failed_check, Mapping) else {}
        finding_summary = str(finding.get("summary") or "").strip() or (
            "An agent-reported check found an issue in the work being reviewed."
        )
        evidence_label = _display_count_label(str(finding.get("evidence_type") or "check"))
        return (
            '<div class="work-feed-finding">'
            '<div class="work-feed-finding-head"><strong>Agent finding</strong>'
            f"<span>{esc(evidence_label)} check</span></div>"
            f"<p>{esc(finding_summary)}</p>"
            '<p class="work-feed-finding-context">This finding is about the work being reviewed, '
            "not agentacct health. No follow-up action was recorded. agentacct does not "
            "guess who should act next.</p></div>"
        )
    elif next_step and state_key in {"blocked", "in_progress"}:
        label = "Decision needed" if state_key == "blocked" else "Next"
        action_parts.append(f"<strong>{label}</strong><p>{esc(next_step)}</p>")
    elif state_key == "blocked" and not blocker:
        action_parts.append(
            "<strong>What to do</strong><p>Open the full activity view to identify the dependency blocking this work.</p>"
        )
    return f'<div class="work-feed-action">{"".join(action_parts)}</div>' if action_parts else ""


def _work_run_identity(item: Mapping[str, Any]) -> tuple[str, str, str] | None:
    client = str(item.get("client") or item.get("reporting_source") or "unknown")
    run_id = str(item.get("run_id") or "")
    if not run_id:
        return None
    return (client, str(item.get("project_dir") or ""), run_id)


def _work_group_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    client = str(item.get("client") or item.get("reporting_source") or "unknown")
    run_identity = _work_run_identity(item)
    if run_identity is not None:
        return ("run", f"{run_identity[0]}\x1f{run_identity[1]}", run_identity[2])
    if item.get("client_session_id"):
        return ("session", client, str(item.get("client_session_id")))
    return ("work", client, str(item.get("work_id") or item.get("section_id") or id(item)))


def _work_group_selection(
    items: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return group state, the state-bearing item, and the title-bearing item."""

    evaluated = [(item, _work_product_state(item)) for item in items]

    def category(state: Mapping[str, Any]) -> int:
        if state.get("action_required"):
            return 0
        if state.get("finding_open"):
            return 1
        if state.get("finding_present"):
            return 2
        if state.get("key") == "in_progress":
            return 3
        if state.get("key") != "verified":
            return 4
        return 5

    best_category = min(category(state) for _item, state in evaluated)
    state_item, group_state = max(
        ((item, state) for item, state in evaluated if category(state) == best_category),
        key=lambda pair: (
            bool(str(pair[0].get("blocker") or "").strip()),
            float(pair[0].get("updated_at") or 0.0),
        ),
    )
    if best_category <= 3:
        title_item = state_item
    else:
        title_item = min(
            items,
            key=lambda item: (
                float(item.get("started_at") or item.get("updated_at") or 0.0),
                str(item.get("section_id") or ""),
            ),
        )
    return dict(group_state), state_item, title_item


def _work_group_sessions(
    items: list[dict[str, Any]],
    session_index: Mapping[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (
            str(item.get("client") or item.get("reporting_source") or ""),
            str(item.get("client_session_id") or ""),
        )
        if not key[1] or key in seen:
            continue
        session = session_index.get(key)
        if session is not None:
            sessions.append(session)
            seen.add(key)
    return sessions


def _work_group_metrics(
    items: list[dict[str, Any]], sessions: list[dict[str, Any]], *, show_session_totals: bool
) -> list[tuple[str, str]]:
    metrics: list[tuple[str, str]] = []
    if sessions and show_session_totals:
        duration = _fmt_duration_hm(sessions[0].get("duration_seconds"))
        if duration:
            metrics.append((duration, "main run duration"))
        usage = _aggregate_session_usage(sessions)
        additive_rows = int((usage.get("additive_rows") if "additive_rows" in usage else usage.get("rows")) or 0)
        excluded_rows = int(usage.get("excluded_non_additive_rows") or 0)
        if additive_rows > 0:
            scope = "run total"
            token_label = "known subtotal tokens" if excluded_rows else "total tokens"
            metrics.append((f"{_fmt_compact_int(usage.get('total_tokens'))} {token_label}", scope))
            if usage.get("estimated_cost_usd") is not None:
                metrics.append((f"{_fmt_optional_usd_text(usage.get('estimated_cost_usd'))} est.", scope))
        if excluded_rows:
            metrics.append((f"{_fmt_int(excluded_rows)} usage row{'s' if excluded_rows != 1 else ''} held", "awaiting source identity or lineage normalization"))
        if len(sessions) > 1:
            metrics.append((f"{_fmt_int(len(sessions) - 1)} supporting", "collapsed sessions"))
        return metrics

    linked_items = [item for item in items if int(item.get("linked_usage_records") or 0) > 0]
    if not linked_items:
        return metrics
    metrics.append(
        (
            f"{_fmt_compact_int(sum(int(item.get('usage_total') or 0) for item in linked_items))} total tokens",
            "attributed usage",
        )
    )
    cost_is_complete = True
    for item in linked_items:
        if "priced_usage_records" in item or "unpriced_usage_records" in item:
            known_rows = int(item.get("priced_usage_records") or 0)
            unavailable_rows = int(item.get("unpriced_usage_records") or 0)
        else:
            breakdown = (
                item.get("cost_confidence_breakdown")
                if isinstance(item.get("cost_confidence_breakdown"), Mapping)
                else {}
            )
            known_rows = sum(
                int(count or 0)
                for basis, count in breakdown.items()
                if str(basis) not in {"unknown", "subscription_unavailable"}
            )
            unavailable_rows = sum(
                int(count or 0)
                for basis, count in breakdown.items()
                if str(basis) in {"unknown", "subscription_unavailable"}
            )
        if known_rows <= 0 or unavailable_rows > 0:
            cost_is_complete = False
            break
    if cost_is_complete:
        metrics.append(
            (
                f"{_fmt_optional_usd_text(sum(float(item.get('estimated_cost_total') or 0.0) for item in linked_items))} est.",
                "attributed cost",
            )
        )
    return metrics


def _work_group_details_html(
    items: list[dict[str, Any]],
    models: list[str],
    sessions: list[dict[str, Any]],
    esc: Any,
    *,
    show_session_totals: bool,
) -> str:
    blocks: list[str] = []
    if len(items) > 1:
        ordered = sorted(items, key=lambda item: float(item.get("started_at") or item.get("updated_at") or 0.0))
        visible = ordered[:8]
        rows = []
        for item in visible:
            state = _work_product_state(item)
            rows.append(
                "<li>"
                f'<span class="status {esc(state.get("css"))}">{esc(state.get("label"))}</span>'
                f'<span>{esc(_work_item_display_title(item))}</span>'
                "</li>"
            )
        overflow = ""
        if len(ordered) > len(visible):
            overflow = f'<p class="note">+{esc(_fmt_int(len(ordered) - len(visible)))} more recorded steps.</p>'
        blocks.append(
            '<div class="run-detail-block"><strong>Recorded work steps</strong>'
            f'<ul class="run-detail-list">{"".join(rows)}</ul>{overflow}</div>'
        )
    if show_session_totals and len(sessions) > 1:
        supporting_sessions = sessions[1:]
        child_count = sum(
            1 for entry in supporting_sessions if str(entry.get("session_kind") or "root") == "child"
        )
        internal_count = sum(
            1 for entry in supporting_sessions if str(entry.get("session_kind") or "root") == "internal"
        )
        other_count = len(supporting_sessions) - child_count - internal_count
        kinds = []
        if child_count:
            kinds.append(f"{_fmt_int(child_count)} child {'session' if child_count == 1 else 'sessions'}")
        if internal_count:
            kinds.append(f"{_fmt_int(internal_count)} internal {'review' if internal_count == 1 else 'reviews'}")
        if other_count:
            kinds.append(
                f"{_fmt_int(other_count)} other supporting {'session' if other_count == 1 else 'sessions'}"
            )
        detail = " · ".join(kinds)
        blocks.append(
            '<div class="run-detail-block"><strong>Collapsed lineage</strong>'
            f'<p class="note">{esc(detail)}. Supporting activity is included in this main run; '
            "each session's own usage is counted once.</p></div>"
        )
    if sessions and any(not str(item.get("client_session_id") or "") for item in items):
        blocks.append(
            '<div class="run-detail-block"><strong>Run association</strong>'
            '<p class="note">Some work steps were grouped through the same reported run id. '
            "The model and usage describe the observed run; they are not allocated to each step.</p></div>"
        )
    if not models:
        blocks.append(
            '<div class="run-detail-block"><strong>Model identity</strong>'
            '<p class="note">No deterministic session/model link was recorded for this work report; agentacct does not guess.</p></div>'
        )
    return "".join(blocks)


def _work_group_feed_item_html(
    items: list[dict[str, Any]],
    *,
    state: Mapping[str, Any],
    state_item: Mapping[str, Any],
    title_item: Mapping[str, Any],
    sessions: list[dict[str, Any]],
    show_session_totals: bool,
    csrf_token: str,
    esc: Any,
) -> str:
    display_client = next(
        (
            str(value)
            for value in [
                *(session.get("client") for session in sessions),
                *(item.get("client") or item.get("reporting_source") for item in items),
            ]
            if value
        ),
        "unknown",
    )
    models: list[str] = []
    for session in sessions:
        for model in _session_model_labels(session):
            if model not in models:
                models.append(model)
    updated_at = max(float(item.get("updated_at") or 0.0) for item in items)
    project = next((str(item.get("project_dir")) for item in items if item.get("project_dir")), "")
    meta = [_human_client(display_client)]
    if project:
        meta.append(project)
    if updated_at:
        meta.append(f"updated {_fmt_time(updated_at)}")
    if sessions and show_session_totals:
        supporting = len(sessions) - 1
        session_scope = (
            f"Main run · {_fmt_int(supporting)} supporting "
            f"{'session' if supporting == 1 else 'sessions'} collapsed"
            if supporting
            else "Main run"
        )
    elif sessions:
        session_scope = f"Reported run · {_fmt_int(len(sessions))} linked {'session' if len(sessions) == 1 else 'sessions'}"
    else:
        session_scope = "Agent work report"
    session_line = f"{session_scope} · {_fmt_int(len(items))} work {'step' if len(items) == 1 else 'steps'}"
    summary = str(title_item.get("summary") or "").strip() or "The agent recorded this work without a summary."
    details_label = f"{_fmt_int(len(items))} work steps" if len(items) > 1 else "Run details"
    status_basis = str(state.get("why") or "This state comes from the saved work ledger.")
    if len(items) > 1:
        status_basis += " Internal sections sharing the same run/session are collapsed into this card."
    finding_episodes: list[Mapping[str, Any]] = []
    seen_finding_targets: set[str] = set()
    for item in items:
        episodes = item.get("finding_episodes") if isinstance(item.get("finding_episodes"), list) else []
        for episode in episodes:
            if not isinstance(episode, Mapping):
                continue
            target_digest = str(episode.get("target_digest") or "")
            if not target_digest or target_digest in seen_finding_targets:
                continue
            seen_finding_targets.add(target_digest)
            finding_episodes.append(episode)
    return _run_card_html(
        title=_work_item_display_title(dict(title_item)),
        client=display_client,
        meta=meta,
        state=state,
        summary=summary,
        status_basis=status_basis,
        metrics=_work_group_metrics(items, sessions, show_session_totals=show_session_totals),
        esc=esc,
        session=sessions[0] if sessions else None,
        models=models,
        session_line=session_line,
        # Finding review + resolve/reopen render inline in the visible card body
        # (action slot), not behind the details expander, so the disposition
        # action is one click away instead of buried.
        action_html=(
            _work_action_html(state_item, state, esc)
            + _finding_episode_controls_html(finding_episodes, csrf_token=csrf_token, esc=esc)
        ),
        details_label=details_label,
        details_extra_html=_work_group_details_html(
            items, models, sessions, esc, show_session_totals=show_session_totals
        ),
        has_work=True,
    )


def _evidence_event_key(event: Mapping[str, Any]) -> tuple[str, ...]:
    return evidence_event_key(event)


def _task_check_events(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Latest machine-check rows visible inside one Task, without duplicates."""

    events: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    items = task.get("work_items") if isinstance(task.get("work_items"), list) else []
    sources = [
        *(item.get("evidence_events") for item in items if isinstance(item, Mapping)),
        task.get("task_evidence_events"),
    ]
    for source in sources:
        if not isinstance(source, list):
            continue
        for event in source:
            if not isinstance(event, Mapping):
                continue
            event_key = "\x1f".join(_evidence_event_key(event))
            if event_key in seen:
                continue
            seen.add(event_key)
            events.append(event)
    return sorted(events, key=lambda event: float(event.get("created_at") or 0.0))


def _task_product_state(
    task: Mapping[str, Any],
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    """Task state may summarize child state, but child titles never become Task titles."""

    items = [
        item
        for item in (task.get("work_items") if isinstance(task.get("work_items"), list) else [])
        if isinstance(item, Mapping)
    ]
    outcome = reduce_task_outcome(task)
    outcome_key = str(outcome.get("key") or "observed")
    latest_checks = [
        event for event in outcome.get("latest_checks", []) if isinstance(event, Mapping)
    ]
    check_carrier: dict[str, Any] = {
        "evidence_events": latest_checks,
        "updated_at": float(outcome.get("max_work_updated_at") or 0.0),
    }
    if outcome_key == "finding":
        attention_state = str(outcome.get("finding_attention_state") or "open")
        if attention_state == "reviewed":
            finding_count = len(outcome.get("findings") or [])
            state = {
                "key": "finding_reviewed",
                "label": "Finding reviewed" if finding_count == 1 else "Findings reviewed",
                "css": "status-missing",
                "rank": 3,
                "action_required": False,
                "finding_open": False,
                "finding_present": True,
                "why": (
                    "Every current finding was reviewed or marked resolved. The failed check evidence remains "
                    "unchanged, and no passing rerun has been recorded."
                ),
            }
        elif attention_state == "resolved":
            state = {
                "key": "finding_resolved",
                "label": "Marked resolved",
                "css": "status-missing",
                "rank": 3,
                "action_required": False,
                "finding_open": False,
                "finding_present": True,
                "why": (
                    "You marked every current finding resolved. This changes attention state only; "
                    "the failed checks are not machine verification."
                ),
            }
        else:
            state = {
                "key": "open_finding",
                "label": "Open finding",
                "css": "status-finding",
                "rank": 2,
                "action_required": False,
                "finding_open": True,
                "finding_present": True,
                "why": (
                    "An agent-reported check found an issue somewhere in this Task. agentacct preserves "
                    "the finding without treating it as a agentacct failure or assigning it to the user."
                ),
            }
        return (
            state,
            check_carrier,
        )
    if outcome_key == "observed":
        return (
            {
                "key": "activity",
                "label": "Observed",
                "css": "status-missing",
                "rank": 4,
                "action_required": False,
                "why": "agentacct observed saved client sessions; no named work outcome was recorded.",
            },
            None,
        )

    _item_state, state_item, _title_item = _work_group_selection([dict(item) for item in items])
    if outcome_key == "resolved":
        return (
            {
                "key": "resolved",
                "label": "Resolved",
                "css": "status-missing",
                "rank": 3,
                "action_required": False,
                "why": (
                    "A later passed check explicitly resolved the exact blocker episode. "
                    "This remains an agent-reported resolution, not a completed or verified outcome."
                ),
            },
            state_item,
        )
    if outcome_key == "verified":
        return (
            {
                "key": "verified",
                "label": "Verified",
                "css": "status-found",
                "rank": 3,
                "action_required": False,
                "why": "Every current work step succeeded and the latest machine checks are newer than the work they verify.",
            },
            check_carrier,
        )
    if outcome_key == "blocked":
        return (
            {
                "key": "blocked",
                "label": "Blocked",
                "css": "status-error",
                "rank": 0,
                "action_required": True,
                "why": "The latest work report says this Task is blocked or failed.",
            },
            state_item,
        )
    if outcome_key == "in_progress":
        return (
            {
                "key": "in_progress",
                "label": "In progress",
                "css": "status-needs-import",
                "rank": 1,
                "action_required": False,
                "why": "At least one recorded work step is still in progress.",
            },
            state_item,
        )
    successful_terminal = all(
        str(item.get("latest_status") or "").lower() in {"completed", "passed"}
        for item in items
    )
    return (
        {
            "key": "reported_done" if successful_terminal else "reported",
            "label": "Completed" if successful_terminal else "Reported",
            "css": "status-missing",
            "rank": 3,
            "action_required": False,
            "why": (
                "All recorded work steps completed, but no current fully passing check set verifies the latest work."
                if successful_terminal
                else "agentacct recorded work, but no verified terminal outcome is current."
            ),
        },
        state_item,
    )


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


def _task_metrics(task: Mapping[str, Any]) -> list[tuple[str, str]]:
    metrics: list[tuple[str, str]] = []
    usage = task.get("usage") if isinstance(task.get("usage"), Mapping) else {}
    additive_rows = int((usage.get("additive_rows") if "additive_rows" in usage else usage.get("rows")) or 0)
    excluded_rows = int(usage.get("excluded_non_additive_rows") or 0)
    if additive_rows > 0:
        write_coverage = str(usage.get("cache_creation_reporting") or "")
        read_coverage = str(usage.get("cache_read_reporting") or "")
        token_note = (
            "cache split partial"
            if write_coverage != "reported" or read_coverage != "reported"
            else "all reported categories"
        )
        volume_parts = [
            "incl. cache",
            f"{_fmt_compact_int(usage.get('fresh_tokens'))} input + output",
        ]
        if int(usage.get("cache_read_tokens") or 0):
            volume_parts.append(
                f"{_fmt_compact_int(usage.get('cache_read_tokens'))} cache reads"
            )
        if int(usage.get("cache_creation_tokens") or 0):
            volume_parts.append(
                f"{_fmt_compact_int(usage.get('cache_creation_tokens'))} cache writes"
            )
        volume_parts.append(token_note)
        metrics.append(
            (
                f"{_fmt_compact_int(usage.get('total_tokens'))} "
                f"{'known subtotal tokens' if excluded_rows else 'total tokens'}",
                " · ".join(volume_parts),
            )
        )
        if usage.get("estimated_cost_usd") is not None:
            metrics.append((f"{_fmt_optional_usd_text(usage.get('estimated_cost_usd'))} est.", "task estimate"))
    if excluded_rows:
        metrics.append((f"{_fmt_int(excluded_rows)} usage row{'s' if excluded_rows != 1 else ''} held", "awaiting source identity or lineage normalization"))
    root_count = len(task.get("root_keys") if isinstance(task.get("root_keys"), list) else [])
    if root_count > 1:
        metrics.append((f"{_fmt_int(root_count)} chats", "linked continuations"))
    work_count = len(task.get("work_items") if isinstance(task.get("work_items"), list) else [])
    if work_count:
        metrics.append((f"{_fmt_int(work_count)} steps", "recorded work"))
    supporting = int(task.get("supporting_count") or 0)
    if supporting:
        metrics.append((f"{_fmt_int(supporting)} supporting", "sessions collapsed"))
    return metrics


def _task_work_preview_html(task: Mapping[str, Any], esc: Any) -> str:
    items = [
        item
        for item in (task.get("work_items") if isinstance(task.get("work_items"), list) else [])
        if isinstance(item, Mapping)
    ]
    if not items:
        return ""

    def rows_html(rows: list[Mapping[str, Any]], *, start_index: int) -> str:
        rendered = []
        for offset, item in enumerate(rows):
            state = _work_product_state(item)
            step_number = start_index + offset
            rendered.append(
                '<li class="task-work-preview-row">'
                f'<span class="task-work-index {esc(state.get("css"))}" aria-hidden="true">{esc(step_number)}</span>'
                f'<span class="task-work-title">{esc(_work_item_display_title(dict(item)))}</span>'
                f'<small>{esc(state.get("label"))}</small>'
                "</li>"
            )
        return "".join(rendered)

    preview_limit = 4
    visible = items[:preview_limit]
    remaining = items[preview_limit:]
    heading = (
        '<div class="task-work-preview-head"><strong>Work steps</strong>'
        f'<span>{esc(_fmt_int(len(items)))} recorded</span></div>'
    )
    visible_list = f'<ol class="task-work-list">{rows_html(visible, start_index=1)}</ol>'
    if not remaining:
        return (
            '<section class="task-work-preview" aria-label="Recorded work steps">'
            f"{heading}{visible_list}</section>"
        )

    remaining_count = len(remaining)
    remaining_noun = "step" if remaining_count == 1 else "steps"
    remaining_label = f"Remaining {_fmt_int(remaining_count)} recorded work " + (
        remaining_noun
    )
    expander = (
        '<details class="task-work-expander"><summary>'
        f'<span class="task-work-expander-closed">Show {esc(_fmt_int(remaining_count))} more {esc(remaining_noun)}</span>'
        '<span class="task-work-expander-open">Hide additional steps</span>'
        "</summary>"
        f'<div class="task-work-overflow" role="region" aria-label="{esc(remaining_label)}" tabindex="0">'
        f'<ol class="task-work-list" start="{esc(preview_limit + 1)}">'
        f"{rows_html(remaining, start_index=preview_limit + 1)}</ol></div></details>"
    )
    return (
        '<section class="task-work-preview" aria-label="Recorded work steps">'
        f"{heading}{visible_list}{expander}</section>"
    )


def _task_session_form_token(secret: str, client: str, session_id: str) -> str:
    material = f"{client}\0{session_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()[:32]


def _finding_form_token(secret: str, event: Mapping[str, Any]) -> str | None:
    target_digest = finding_target_digest(event)
    if not secret or target_digest is None:
        return None
    material = f"finding-disposition-v1\0{target_digest}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()[:32]


def _finding_episode_controls_html(
    episodes: Any,
    *,
    csrf_token: str,
    esc: Any,
) -> str:
    if not isinstance(episodes, list) or not episodes:
        return ""

    def hidden(name: str, value: Any) -> str:
        return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'

    rows: list[str] = []
    ordered = sorted(
        (episode for episode in episodes if isinstance(episode, Mapping)),
        key=lambda episode: (
            0 if episode.get("attention_open") else 1,
            -float(episode.get("opened_at") or 0.0),
        ),
    )
    for episode in ordered:
        event = episode.get("failure_event") if isinstance(episode.get("failure_event"), Mapping) else {}
        disposition = (
            episode.get("latest_disposition")
            if isinstance(episode.get("latest_disposition"), Mapping)
            else {}
        )
        state = str(episode.get("disposition_state") or "open")
        label = {
            "reviewed": "Reviewed",
            "resolved": "Marked resolved",
        }.get(state, "Open finding")
        css = "status-finding" if state == "open" else "status-missing"
        summary = str(event.get("summary") or "").strip() or "A failed check was recorded."
        evidence_label = _display_count_label(str(event.get("evidence_type") or "check"))
        observed = _fmt_time(event.get("created_at"))
        token = str(episode.get("finding_token") or "")
        revision = int(episode.get("revision") or 0)
        note = str(disposition.get("note") or "").strip()
        note_html = f'<p><strong>Disposition note:</strong> {esc(note)}</p>' if note else ""
        if state == "reviewed":
            truth_copy = "Reviewed by you; no passing rerun has been recorded."
        elif state == "resolved":
            truth_copy = "Marked resolved by you; this is not machine verification."
        else:
            truth_copy = "The failed/error result remains current objective evidence."
        controls = ""
        source_type = str(event.get("source_type") or "")
        chain_valid = disposition.get("chain_valid") is not False
        if token and csrf_token and source_type in {"mcp_agent_reported", "client_hook"} and chain_valid:
            common = (
                hidden("csrf_token", csrf_token)
                + hidden("finding_token", token)
                + hidden("expected_revision", revision)
            )
            forms: list[str] = []
            if state == "open":
                forms.append(
                    '<form method="post" action="/findings/disposition">'
                    + common
                    + hidden("action", "mark_reviewed")
                    + '<button class="task-control-button" type="submit">Mark reviewed</button></form>'
                )
            if state in {"open", "reviewed"}:
                forms.append(
                    '<form method="post" action="/findings/disposition" class="finding-review-note">'
                    + common
                    + hidden("action", "resolve")
                    + '<input name="note" maxlength="1200" required placeholder="Why is this resolved?" '
                    + 'aria-label="Finding resolution note">'
                    + '<button class="task-control-button" type="submit">Mark resolved</button></form>'
                )
            if state in {"reviewed", "resolved"}:
                forms.append(
                    '<form method="post" action="/findings/disposition">'
                    + common
                    + hidden("action", "reopen")
                    + '<button class="task-control-button is-danger" type="submit">Reopen</button></form>'
                )
            controls = '<div class="finding-review-actions">' + "".join(forms) + "</div>"
        elif not chain_valid:
            controls = (
                '<p class="note">Disposition history is conflicting or corrupt. agentacct reopened the attention state; '
                "inspect Advanced before acting.</p>"
            )
        row_id = f' id="finding-{esc(token)}"' if token else ""
        rows.append(
            f'<article class="finding-review-row"{row_id}>'
            '<div class="finding-review-head">'
            f'<strong>{esc(evidence_label)} check{(" · " + esc(observed)) if observed else ""}</strong>'
            f'<span class="status {esc(css)}">{esc(label)}</span></div>'
            f"<p>{esc(summary)}</p><p class=\"note\">{esc(truth_copy)}</p>{note_html}{controls}</article>"
        )
    if not rows:
        return ""
    return (
        '<div class="run-detail-block finding-review"><strong>Finding review</strong>'
        '<p class="note">Disposition changes attention only. The original failed/error evidence stays in history, '
        "and only a later same-check pass can provide machine resolution.</p>"
        '<div class="finding-review-list">' + "".join(rows) + "</div></div>"
    )


def _task_controls_html(
    task: Mapping[str, Any],
    *,
    candidate_tasks: list[Mapping[str, Any]],
    csrf_token: str,
    esc: Any,
) -> str:
    primary = task.get("primary_root") if isinstance(task.get("primary_root"), Mapping) else {}
    source_client = str(primary.get("client") or "")
    source_session = str(primary.get("client_session_id") or "")
    if not source_client or not source_session or not csrf_token:
        return ""

    def hidden(name: str, value: Any) -> str:
        return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'

    link_rows: list[str] = []
    source_sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    source_root_row = next(
        (row for row in source_sessions if _session_identity_key(row) == (source_client, source_session)),
        {},
    )
    source_project = str(source_root_row.get("project") or "") if isinstance(source_root_row, Mapping) else ""
    for candidate in candidate_tasks[:6]:
        target = candidate.get("primary_root") if isinstance(candidate.get("primary_root"), Mapping) else {}
        target_client = str(target.get("client") or "")
        target_session = str(target.get("client_session_id") or "")
        if not target_client or not target_session:
            continue
        candidate_sessions = candidate.get("sessions") if isinstance(candidate.get("sessions"), list) else []
        target_row = next(
            (row for row in candidate_sessions if _session_identity_key(row) == (target_client, target_session)),
            {},
        )
        target_project = str(target_row.get("project") or "") if isinstance(target_row, Mapping) else ""
        cross_scope = target_client != source_client or (
            bool(source_project and target_project) and target_project != source_project
        )
        warning = " · cross-scope confirmation" if cross_scope else ""
        label = _task_title(candidate)
        link_rows.append(
            '<form method="post" action="/tasks/link" class="task-control-row">'
            + hidden("csrf_token", csrf_token)
            + hidden("session_token", _task_session_form_token(csrf_token, source_client, source_session))
            + hidden(
                "target_session_token",
                _task_session_form_token(csrf_token, target_client, target_session),
            )
            + hidden("confirm_cross_scope", "true" if cross_scope else "false")
            + f'<span><strong>{esc(label)}</strong><small>{esc(_human_client(target_client) + warning)}</small></span>'
            + '<button class="task-control-button" type="submit">Link continuation</button></form>'
        )

    management: list[str] = []
    continuation_id = str(task.get("continuation_id") or "")
    if continuation_id:
        management.append(
            '<form method="post" action="/tasks/rename" class="task-rename-form">'
            + hidden("csrf_token", csrf_token)
            + hidden("task_id", continuation_id)
            + f'<input name="title" maxlength="160" value="{esc(task.get("title_override") or "")}" '
            + 'placeholder="Name this task" aria-label="Task title">'
            + '<button class="task-control-button" type="submit">Save name</button></form>'
        )
        roots = task.get("root_keys") if isinstance(task.get("root_keys"), list) else []
        for index, root in enumerate(roots):
            if not isinstance(root, Mapping):
                continue
            client = str(root.get("client") or "")
            session_id = str(root.get("client_session_id") or "")
            if not client or not session_id:
                continue
            role = "Primary chat" if index == 0 else f"Continuation {index}"
            management.append(
                '<form method="post" action="/tasks/unlink" class="task-control-row">'
                + hidden("csrf_token", csrf_token)
                + hidden("session_token", _task_session_form_token(csrf_token, client, session_id))
                + f'<span><strong>{esc(role)}</strong><small>{esc(_human_client(client))}</small></span>'
                + '<button class="task-control-button is-danger" type="submit">Unlink chat</button></form>'
            )
    if not link_rows and not management:
        return ""
    return (
        '<div class="run-detail-block task-controls"><strong>Manage task</strong>'
        '<p class="note">Link only when a new chat continues the same user task. Raw sessions and usage stay unchanged.</p>'
        f'{"".join(management)}{"".join(link_rows)}</div>'
    )


def _task_details_html(
    task: Mapping[str, Any],
    *,
    candidate_tasks: list[Mapping[str, Any]],
    csrf_token: str,
    esc: Any,
) -> str:
    blocks: list[str] = []
    checks = _task_check_events(task)
    if checks:
        rows = []
        for event in checks[-10:]:
            result = str(event.get("result") or "unknown").lower()
            css = "status-found" if result == "passed" else "status-error" if result in {"failed", "error"} else "status-missing"
            label = "Passed" if result == "passed" else "Failed" if result in {"failed", "error"} else "Unknown"
            summary = _evidence_note_for_item({"evidence_events": [event]})
            rows.append(
                "<li>"
                f'<span class="status {esc(css)}">{esc(label)}</span>'
                f'<span>{esc(summary)}</span>'
                "</li>"
            )
        blocks.append(
            '<div class="run-detail-block"><strong>Checks</strong>'
            f'<ul class="run-detail-list task-detail-list">{"".join(rows)}</ul></div>'
        )
    if task.get("has_session_unlinked_work"):
        count = int(task.get("session_unlinked_work_count") or 0)
        blocks.append(
            '<div class="run-detail-block"><strong>Task-level association</strong>'
            f'<p class="note">{esc(_fmt_int(count))} work {"report" if count == 1 else "reports"} '
            "appeared in more than one client log, but every observation resolves to this Task. "
            "agentacct groups the work here without choosing a specific session or allocating that session's tokens.</p></div>"
        )
    if not list(task.get("models") or []):
        blocks.append(
            '<div class="run-detail-block"><strong>Model identity</strong>'
            '<p class="note">No deterministic session/model link was recorded for this Task; '
            "agentacct does not guess.</p></div>"
        )
    roots = task.get("root_keys") if isinstance(task.get("root_keys"), list) else []
    supporting = int(task.get("supporting_count") or 0)
    if roots or supporting:
        root_copy = f"{_fmt_int(len(roots))} root {'chat' if len(roots) == 1 else 'chats'}"
        if supporting:
            root_copy += f" · {_fmt_int(supporting)} supporting {'session' if supporting == 1 else 'sessions'}"
        task_usage = task.get("usage") if isinstance(task.get("usage"), Mapping) else {}
        held_rows = int(task_usage.get("excluded_non_additive_rows") or 0)
        usage_copy = (
            f" Proven additive rows contribute once; {_fmt_int(held_rows)} usage "
            f"row{' is' if held_rows == 1 else 's are'} held for source identity or lineage normalization."
            if held_rows
            else " Every proven-additive session row contributes once."
        )
        blocks.append(
            '<div class="run-detail-block"><strong>Session structure</strong>'
            f'<p class="note">{esc(root_copy)}.{esc(usage_copy)}</p></div>'
        )
    # Finding review/resolve controls now render inline in the card body (see
    # _task_feed_item_html action slot); only task-link controls stay here.
    blocks.append(
        _task_controls_html(
            task,
            candidate_tasks=candidate_tasks,
            csrf_token=csrf_token,
            esc=esc,
        )
    )
    return "".join(blocks)


def _task_sessions_overflow_html(task: Mapping[str, Any], esc: Any) -> str:
    """Expandable, scrollable per-session list for one Task card (Dashboard v2).

    Root chat first, then the task's sessions by real estimated cost (unpriced
    sessions keep 'No estimate', never a fabricated $0). Uses the shared
    ``task-work-overflow`` scroll container so long tasks scroll instead of
    stretching the card; a NEW row class (not ``session-row``, which the
    Overview must never emit) keeps the /sessions marker exclusive to /sessions.
    """

    sessions = [
        session
        for session in (task.get("sessions") if isinstance(task.get("sessions"), list) else [])
        if isinstance(session, Mapping)
    ]
    if not sessions:
        return ""
    primary = task.get("primary_root") if isinstance(task.get("primary_root"), Mapping) else {}
    primary_key = (str(primary.get("client") or ""), str(primary.get("client_session_id") or ""))

    def sort_key(session: Mapping[str, Any]) -> tuple[int, float, int]:
        usage = session.get("usage") if isinstance(session.get("usage"), Mapping) else {}
        cost = usage.get("estimated_cost_usd")
        tokens = int(usage.get("total_tokens") or 0)
        is_root = _session_identity_key(session) == primary_key
        return (0 if is_root else 1, -(float(cost) if cost is not None else -1.0), -tokens)

    ordered = sorted(sessions, key=sort_key)
    cap = 60
    shown = ordered[:cap]
    rows: list[str] = []
    for session in shown:
        client = str(session.get("client") or "")
        usage = session.get("usage") if isinstance(session.get("usage"), Mapping) else {}
        is_root = _session_identity_key(session) == primary_key
        role = "root" if is_root else _record_kind_label(session.get("session_kind"))
        role_display = {
            "root": "Root chat",
            "main": "Root chat",
            "child": "Child session",
            "auto-review": "Auto-review session",
        }.get(role, f"{role} session")
        # Product home never displays a session-id fragment (locked decision);
        # a titleless session shows its role, not a raw id.
        title = str(session.get("client_session_title") or "").strip() or role_display
        cost = usage.get("estimated_cost_usd")
        cost_text = _fmt_usd(cost) if cost is not None else "No estimate"
        tokens = int(usage.get("total_tokens") or 0)
        lane_class = client_lane_class(client)
        rows.append(
            '<div class="ov-sess-row">'
            f'<span class="agent-avatar {esc(lane_class)} is-mini" aria-hidden="true">{esc(_agent_mark(client))}</span>'
            f'<div class="ov-sess-copy"><strong>{esc(_short_text(title, max_length=52))}</strong>'
            f'<span>{esc(role_display)}</span></div>'
            f'<div class="ov-sess-cost"><strong>{esc(cost_text)}</strong>'
            f'<span>{esc(_fmt_compact_int(tokens))} tok</span></div>'
            "</div>"
        )
    count = len(sessions)
    cap_note = (
        ""
        if len(shown) == count
        else f'<div class="ov-sess-cap note">Showing {_fmt_int(len(shown))} of {_fmt_int(count)} '
        "sessions; open the task for the rest.</div>"
    )
    summary = f"{_fmt_int(count)} session{'' if count == 1 else 's'} in this task"
    return (
        f'<details class="ov-task-sessions"><summary>{esc(summary)}</summary>'
        f'<div class="task-work-overflow ov-sess-scroll">{"".join(rows)}{cap_note}</div>'
        "</details>"
    )


def _task_feed_item_html(
    task: Mapping[str, Any],
    *,
    candidate_tasks: list[Mapping[str, Any]],
    csrf_token: str,
    esc: Any,
) -> str:
    state, state_item = _task_product_state(task)
    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    primary = task.get("primary_root") if isinstance(task.get("primary_root"), Mapping) else {}
    primary_key = (str(primary.get("client") or ""), str(primary.get("client_session_id") or ""))
    root_session = next(
        (session for session in sessions if isinstance(session, Mapping) and _session_identity_key(session) == primary_key),
        sessions[0] if sessions else None,
    )
    clients: list[str] = []
    for session in sessions:
        client = str(session.get("client") or "") if isinstance(session, Mapping) else ""
        if client and client not in clients:
            clients.append(client)
    display_client = clients[0] if clients else str(primary.get("client") or "unknown")
    updated_at = max(
        float(task.get("last_activity_at") or 0.0),
        max(
            (
                float(item.get("updated_at") or 0.0)
                for item in task.get("work_items", [])
                if isinstance(item, Mapping)
            ),
            default=0.0,
        ),
    )
    meta = [" + ".join(_human_client(client) for client in clients) or _human_client(display_client)]
    if isinstance(root_session, Mapping) and root_session.get("project"):
        meta.append(str(root_session.get("project")))
    if updated_at:
        meta.append(f"updated {_fmt_time(updated_at)}")
    roots = task.get("root_keys") if isinstance(task.get("root_keys"), list) else []
    root_count = len(roots)
    session_line = (
        f"{_fmt_int(root_count)} chats linked"
        if root_count > 1
        else "1 root chat"
    )
    supporting = int(task.get("supporting_count") or 0)
    if supporting:
        session_line += f" · {_fmt_int(supporting)} supporting {'session' if supporting == 1 else 'sessions'} collapsed"
    work_count = len(task.get("work_items") if isinstance(task.get("work_items"), list) else [])
    if work_count:
        session_line += f" · {_fmt_int(work_count)} work {'step' if work_count == 1 else 'steps'}"
    check_count = len(_task_check_events(task))
    if check_count:
        session_line += f" · {_fmt_int(check_count)} {'check' if check_count == 1 else 'checks'}"
    summary = (
        str(state_item.get("summary") or "").strip()
        if isinstance(state_item, Mapping)
        else ""
    ) or "This Task is anchored to the observed root client chat; supporting sessions and recorded work are nested below."
    action_html = _task_work_preview_html(task, esc)
    if isinstance(state_item, Mapping) and (
        state.get("action_required")
        or state.get("finding_open")
        or state.get("key") == "resolved"
    ):
        action_html += _work_action_html(state_item, state, esc)
    # Inline finding review + resolve/reopen in the visible card body, not the
    # details expander, so the disposition action is one click away.
    action_html += _finding_episode_controls_html(
        task.get("finding_episodes"), csrf_token=csrf_token, esc=esc
    )
    return _run_card_html(
        title=_task_title(task),
        client=display_client,
        meta=meta,
        state=state,
        summary=summary,
        status_basis=str(state.get("why") or "This state comes from the Task's recorded work and checks."),
        metrics=_task_metrics(task),
        esc=esc,
        session=root_session if isinstance(root_session, Mapping) else None,
        models=list(task.get("models") or []),
        session_line=session_line,
        action_html=action_html,
        details_label="Task details & controls",
        details_extra_html=_task_details_html(
            task,
            candidate_tasks=candidate_tasks,
            csrf_token=csrf_token,
            esc=esc,
        ),
        sessions_html=_task_sessions_overflow_html(task, esc),
        has_work=bool(work_count),
        title_href=(
            "/tasks/" + quote(str(task.get("public_task_id")))
            if task.get("public_task_id")
            else None
        ),
    )


def _unassigned_finding_feed_item_html(
    finding: Mapping[str, Any],
    *,
    csrf_token: str,
    esc: Any,
) -> str:
    """Render one open failure that agentacct refused to force into a Task."""

    event = finding.get("event") if isinstance(finding.get("event"), Mapping) else {}
    episode = finding.get("episode") if isinstance(finding.get("episode"), Mapping) else {}
    disposition_state = str(episode.get("disposition_state") or "open")
    state_label = {
        "reviewed": "Finding reviewed",
        "resolved": "Marked resolved",
    }.get(disposition_state, "Open finding")
    state = {
        "key": "open_finding" if disposition_state == "open" else f"finding_{disposition_state}",
        "label": state_label,
        "css": "status-finding" if disposition_state == "open" else "status-missing",
        "rank": 2,
        "action_required": False,
        "finding_open": disposition_state == "open",
        "finding_present": True,
        "why": str(finding.get("reason") or "agentacct could not safely assign this check to one Task."),
    }
    client = str(event.get("client") or event.get("source") or "unknown")
    meta = [_human_client(client)]
    if event.get("project_dir"):
        meta.append(str(event.get("project_dir")))
    if event.get("created_at"):
        meta.append(f"observed {_fmt_time(event.get('created_at'))}")
    item = {
        "evidence_events": [event],
        "updated_at": 0,
    }
    assignment_reason = str(
        finding.get("reason")
        or "agentacct could not safely assign this check to one Task."
    )
    return _run_card_html(
        title="Unassigned agent finding",
        client=client,
        meta=meta,
        state=state,
        summary=assignment_reason,
        status_basis=(
            "The failed check is preserved, but its Task association is unavailable or ambiguous; "
            "agentacct did not guess."
        ),
        metrics=[],
        esc=esc,
        session_line="Task association unavailable",
        # Resolve/review the unassigned finding inline in the visible card body,
        # so it can be dispositioned in one click instead of via the expander.
        action_html=(
            _work_action_html(item, state, esc)
            + _finding_episode_controls_html(
                [finding.get("episode")] if isinstance(finding.get("episode"), Mapping) else [],
                csrf_token=csrf_token,
                esc=esc,
            )
        ),
        details_label="Finding details",
        details_extra_html=(
            '<div class="run-detail-block"><strong>Assignment</strong>'
            f'<p class="note">{esc(assignment_reason)}</p></div>'
        ),
        has_work=False,
    )


def _is_collapsible_supporting_review(entry: Mapping[str, Any]) -> bool:
    items = entry.get("items") if isinstance(entry.get("items"), list) else []
    sessions = entry.get("sessions") if isinstance(entry.get("sessions"), list) else []
    state = entry.get("state") if isinstance(entry.get("state"), Mapping) else {}
    return bool(items) and bool(sessions) and all(
        str(item.get("kind") or "") == "review" and str(item.get("latest_status") or "") == "completed"
        for item in items
        if isinstance(item, Mapping)
    ) and all(
        str(session.get("session_kind") or "root") in {"child", "internal"}
        for session in sessions
        if isinstance(session, Mapping)
    ) and not state.get("action_required") and not state.get("finding_open") and state.get("key") != "in_progress"


def _visible_attention_entries(
    entries: list[dict[str, Any]],
    *,
    minimum: int = 10,
) -> list[dict[str, Any]]:
    """Keep every blocker/current finding, then fill the ordinary recent-card budget."""

    required_ids = {
        id(entry)
        for entry in entries
        if isinstance(entry.get("state"), Mapping)
        and (
            entry["state"].get("action_required")
            or entry["state"].get("finding_open")
            or entry["state"].get("finding_present")
        )
    }
    selected_ids = set(required_ids)
    for entry in entries:
        if len(selected_ids) >= max(minimum, len(required_ids)):
            break
        selected_ids.add(id(entry))
    return [entry for entry in entries if id(entry) in selected_ids]


def _supporting_review_rollup_html(entries: list[dict[str, Any]], esc: Any) -> str:
    if not entries:
        return ""
    ordered = sorted(entries, key=lambda entry: -float(entry.get("updated_at") or 0.0))
    visible = ordered[:12]
    rows = []
    for entry in visible:
        state = entry.get("state") if isinstance(entry.get("state"), Mapping) else {}
        title_item = entry.get("title_item") if isinstance(entry.get("title_item"), Mapping) else {}
        client = title_item.get("client") or title_item.get("reporting_source") or "unknown"
        updated = _fmt_time(entry.get("updated_at"))
        meta = _human_client(client) + (f" · updated {updated}" if updated else "")
        rows.append(
            "<li>"
            f'<span class="status {esc(state.get("css"))}">{esc(state.get("label"))}</span>'
            f'<div><strong>{esc(_work_item_display_title(dict(title_item)))}</strong><small>{esc(meta)}</small></div>'
            "</li>"
        )
    overflow = ""
    if len(ordered) > len(visible):
        overflow = (
            '<li><span class="supporting-review-count">+'
            f'{esc(_fmt_int(len(ordered) - len(visible)))}</span><div><strong>More completed reviews</strong>'
            '<small>Open All activity for the complete ledger.</small></div></li>'
        )
    count = len(entries)
    return (
        '<details class="supporting-review-rollup">'
        '<summary><span><strong>'
        f'{esc(_fmt_int(count))} supporting {"review" if count == 1 else "reviews"}'
        '</strong><span>Completed internal review runs are collapsed so primary work stays readable.</span></span>'
        f'<span class="supporting-review-count">{esc(_fmt_int(count))}</span></summary>'
        f'<ul class="supporting-review-list">{"".join(rows)}{overflow}</ul></details>'
    )


def _work_feed_item_html(
    item: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    esc: Any,
    session: Mapping[str, Any] | None = None,
) -> str:
    title = _work_item_display_title(dict(item))
    updated = _fmt_time(item.get("updated_at"))
    display_client = (session or {}).get("client") if isinstance(session, Mapping) else None
    display_client = display_client or item.get("client") or item.get("reporting_source")
    meta: list[str] = []
    if display_client:
        meta.append(_human_client(display_client))
    if item.get("project_dir"):
        meta.append(str(item.get("project_dir")))
    if updated:
        meta.append(f"updated {updated}")
    summary = str(item.get("summary") or "").strip() or "The agent recorded this work without a summary."

    metrics = _work_item_run_metrics(item, session)
    return _run_card_html(
        title=title,
        client=display_client,
        meta=meta,
        state=state,
        summary=summary,
        status_basis=str(state.get("why") or "This state comes from the saved work ledger."),
        metrics=metrics,
        esc=esc,
        session=session,
        action_html=_work_action_html(item, state, esc),
        has_work=True,
    )


def _activity_feed_item_html(entry: Mapping[str, Any], *, esc: Any) -> str:
    goal = _session_goals_label(dict(entry))
    title = goal if goal != "—" else f"{_human_client(entry.get('client'))} activity"
    updated = _fmt_time(entry.get("last_activity_at"))
    meta = [_human_client(entry.get("client"))]
    if entry.get("project"):
        meta.append(str(entry.get("project")))
    if updated:
        meta.append(f"updated {updated}")
    summary = (
        "Local agent activity was observed for this session."
        if goal != "—"
        else "agentacct observed local agent activity; no named task was reported for this session."
    )
    state = {
        "key": "activity",
        "label": "Activity observed",
        "css": "status-missing",
        "action_required": False,
    }
    return _run_card_html(
        title=title,
        client=entry.get("client"),
        meta=meta,
        state=state,
        summary=summary,
        status_basis="This card comes from saved local session activity, not from an agent-reported task outcome.",
        metrics=_session_run_metrics(entry),
        esc=esc,
        session=entry,
        has_work=False,
    )


def _root_group_metrics(group: Mapping[str, Any]) -> list[tuple[str, str]]:
    root = group.get("root") if isinstance(group.get("root"), Mapping) else {}
    lineage_state = str(group.get("lineage_state") or "resolved_root")
    metrics: list[tuple[str, str]] = []
    duration = _fmt_duration_hm(root.get("duration_seconds"))
    is_residual = lineage_state in {"residual_root", "residual_supporting"}
    if duration and lineage_state != "residual_supporting":
        metrics.append(
            (duration, "main run duration" if lineage_state == "resolved_root" else "observed session duration")
        )
    usage = group.get("usage") if isinstance(group.get("usage"), Mapping) else {}
    additive_rows = int((usage.get("additive_rows") if "additive_rows" in usage else usage.get("rows")) or 0)
    excluded_rows = int(usage.get("excluded_non_additive_rows") or 0)
    if additive_rows > 0:
        usage_scope = (
            "remaining activity"
            if is_residual
            else "run total"
            if lineage_state == "resolved_root"
            else "lineage group total"
        )
        token_label = "known subtotal tokens" if excluded_rows else "total tokens"
        metrics.append((f"{_fmt_compact_int(usage.get('total_tokens'))} {token_label}", usage_scope))
        if usage.get("estimated_cost_usd") is not None:
            metrics.append((f"{_fmt_optional_usd_text(usage.get('estimated_cost_usd'))} est.", usage_scope))
    if excluded_rows:
        metrics.append((f"{_fmt_int(excluded_rows)} usage row{'s' if excluded_rows != 1 else ''} held", "awaiting source identity or lineage normalization"))
    supporting = int(group.get("supporting_count") or 0)
    if supporting:
        metrics.append(
            (
                f"{_fmt_int(supporting)} "
                f"{'remaining' if lineage_state == 'residual_supporting' else 'supporting'} "
                f"{'session' if supporting == 1 else 'sessions'}",
                "remaining lineage"
                if is_residual
                else "collapsed lineage"
                if lineage_state == "resolved_root"
                else "grouped lineage",
            )
        )
    return metrics


def _root_group_details_html(group: Mapping[str, Any], esc: Any) -> str:
    supporting = int(group.get("supporting_count") or 0)
    if not supporting:
        return ""
    child_count = int(group.get("child_count") or 0)
    internal_count = int(group.get("internal_count") or 0)
    parts = []
    if child_count:
        parts.append(f"{_fmt_int(child_count)} child {'session' if child_count == 1 else 'sessions'}")
    if internal_count:
        parts.append(f"{_fmt_int(internal_count)} internal {'review' if internal_count == 1 else 'reviews'}")
    other_count = supporting - child_count - internal_count
    if other_count:
        parts.append(
            f"{_fmt_int(other_count)} other supporting {'session' if other_count == 1 else 'sessions'}"
        )
    detail = " · ".join(parts)
    if str(group.get("lineage_state") or "") in {"residual_root", "residual_supporting"}:
        return (
            '<div class="run-detail-block"><strong>Remaining lineage</strong>'
            f'<p class="note">{esc(detail)}. Linked work is shown separately; this card includes only '
            "the remaining sessions, with each session's own usage counted once.</p></div>"
        )
    if str(group.get("lineage_state") or "") in {"cycle", "orphan_parent_missing"}:
        return (
            '<div class="run-detail-block"><strong>Grouped lineage</strong>'
            f'<p class="note">{esc(detail)}. These sessions remain in one top-level lineage group; '
            "each session's own usage is counted once.</p></div>"
        )
    return (
        '<div class="run-detail-block"><strong>Collapsed lineage</strong>'
        f'<p class="note">{esc(detail)}. Supporting activity is included in this main run; '
        "each session's own usage is counted once.</p></div>"
    )


def _root_activity_feed_item_html(group: Mapping[str, Any], *, esc: Any) -> str:
    root = group.get("root") if isinstance(group.get("root"), Mapping) else {}
    work_titles = group.get("work_titles") if isinstance(group.get("work_titles"), list) else []
    goal = _session_goals_label(dict(root))
    title = str(work_titles[0]) if work_titles else goal
    if not title or title == "—":
        title = f"{_human_client(root.get('client'))} activity"
    updated = _fmt_time(group.get("last_activity_at"))
    meta = [_human_client(root.get("client"))]
    if root.get("project"):
        meta.append(str(root.get("project")))
    if updated:
        meta.append(f"updated {updated}")
    supporting = int(group.get("supporting_count") or 0)
    lineage_state = str(group.get("lineage_state") or "resolved_root")
    if lineage_state == "cycle":
        session_line = "Lineage group · cycle collapsed"
    elif lineage_state == "orphan_parent_missing":
        session_line = "Top-level supporting session · parent not observed"
    elif lineage_state == "residual_root":
        session_line = "Main run · linked work shown separately"
    elif lineage_state == "residual_supporting":
        session_line = "Supporting activity · linked run shown separately"
    else:
        session_line = "Main run"
    if supporting:
        if lineage_state == "residual_supporting":
            session_line += f" · {_fmt_int(supporting)} remaining {'session' if supporting == 1 else 'sessions'}"
        else:
            session_line += (
                f" · {_fmt_int(supporting)} supporting "
                f"{'session' if supporting == 1 else 'sessions'} collapsed"
            )
    if lineage_state in {"residual_root", "residual_supporting"}:
        summary = "Remaining local activity is grouped here; linked work from this lineage is shown separately."
    elif supporting:
        summary = "Local agent activity was observed for this run. Supporting sessions are grouped under the main run."
    else:
        summary = "Local agent activity was observed for this run."
    state = {
        "key": "activity",
        "label": "Run observed",
        "css": "status-missing",
        "action_required": False,
    }
    return _run_card_html(
        title=title,
        client=root.get("client"),
        meta=meta,
        state=state,
        summary=summary,
        status_basis=(
            "This card comes from saved local session activity. agentacct resolves parent lineage "
            "transitively and sums only proven-additive usage rows once; held source-conflicted or lineage-dependent rows remain visible."
            + (
                " A lineage cycle was preserved and collapsed deterministically."
                if lineage_state == "cycle"
                else " The claimed parent session was not observed in this workspace."
                if lineage_state == "orphan_parent_missing"
                else " Linked work is shown separately; this card contains only the remaining lineage activity."
                if lineage_state in {"residual_root", "residual_supporting"}
                else ""
            )
        ),
        metrics=_root_group_metrics(group),
        esc=esc,
        session=root,
        models=list(group.get("models") or []),
        session_line=session_line,
        details_label=(
            f"{_fmt_int(supporting)} supporting {'session' if supporting == 1 else 'sessions'}"
            if supporting
            else "Run details"
        ),
        details_extra_html=_root_group_details_html(group, esc),
        has_work=False,
    )


def _overview_scoped_usage_records(
    data: _DashboardPageData, identity_sessions: list[dict[str, Any]]
) -> list[DashboardUsageRecord]:
    """Saved + held usage rows belonging to the exact in-scope session
    identities. One definition so the all-history pulse totals and the 30-day
    charts never scope a different population."""

    identity_keys = {_session_identity_key(entry) for entry in identity_sessions}
    return [
        record
        for record in [*data.usage_view.saved_records, *data.usage_view.excluded_saved_records]
        if (str(record.client or ""), str(record.session_id or "")) in identity_keys
    ]


def _overview_usage_totals(
    data: _DashboardPageData, identity_sessions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the homepage cube from exact in-scope session identities."""

    return build_usage_cube(
        _overview_scoped_usage_records(data, identity_sessions),
        record_time=_usage_record_time,
        days=None,
        granularity="weekly",
        today=date.today(),
    )["totals"]


def _overview_usage_pulse_html(
    totals: Mapping[str, Any], session_groups: list[dict[str, Any]], esc: Any
) -> str:
    rows = int(totals.get("rows") or 0)
    additive_rows = int(totals.get("additive_rows") or 0)
    excluded_rows = int(totals.get("excluded_non_additive_rows") or 0)
    main_runs = sum(
        1 for group in session_groups if str(group.get("lineage_state") or "resolved_root") == "resolved_root"
    )
    unresolved_groups = len(session_groups) - main_runs
    supporting = sum(int(group.get("supporting_count") or 0) for group in session_groups)
    if not rows:
        main = (
            '<div class="empty-state">No saved usage rows are linked to this workspace yet. '
            "Run the local usage import to add token and cost evidence; agentacct will not invent zeros.</div>"
        )
    elif not additive_rows and excluded_rows:
        main = (
            '<div class="empty-state"><strong>Usage normalization in progress.</strong> '
            f'{esc(_fmt_int(excluded_rows))} usage '
            f'{"row is" if excluded_rows == 1 else "rows are"} preserved but held from token and cost totals. '
            'agentacct will not present the unknown exclusive usage as zero.</div>'
        )
    else:
        uncached_input = int(totals.get("input_tokens") or 0)
        output = int(totals.get("output_tokens") or 0)
        cache_write = int(totals.get("cache_creation_tokens") or 0)
        cache_read = int(totals.get("cache_read_tokens") or 0)
        composition_total = int(totals.get("total_tokens_including_cached") or 0)

        def share(value: int) -> str:
            return f"{(value / composition_total * 100) if composition_total else 0:.4f}%"

        estimated_cost = totals.get("estimated_cost_usd")
        unpriced_rows = int(totals.get("unpriced_rows") or 0)
        cache_write_reporting = str(totals.get("cache_creation_reporting") or "")
        cache_read_reporting = str(totals.get("cache_read_reporting") or "")
        known_additive_cost = totals.get("known_additive_cost_usd")
        if estimated_cost is None and excluded_rows and known_additive_cost is not None:
            cost_value = _fmt_optional_usd_text(known_additive_cost)
            cost_label = "Known cost subtotal"
        elif estimated_cost is None:
            cost_value = "No estimate"
            cost_label = "Cost unavailable"
        else:
            cost_value = _fmt_optional_usd_text(estimated_cost)
            cost_label = "Partial est. cost" if unpriced_rows else "Est. API cost"
        confidence = str(totals.get("cost_confidence_label") or "no cost estimate")
        confidence = (
            confidence.replace("mixed confidence", "mixed cost basis")
            .replace("estimated_from_tokens", "token-price estimate")
            .replace("client_reported", "client-reported")
            .replace("provider_billed", "provider-billed")
            .replace("unknown confidence", "unknown cost basis")
        )
        if confidence.endswith(" confidence"):
            confidence = confidence[: -len(" confidence")] + " basis"
        coverage = (
            f"{_fmt_int(totals.get('priced_rows'))}/{_fmt_int(additive_rows)} additive rows priced"
            if estimated_cost is not None
            else "complete cost unavailable" if excluded_rows else "no priced rows"
        )
        if cache_write_reporting == "not_reported":
            cache_write_copy = "cache writes not reported by this source"
        elif cache_write_reporting == "unknown":
            cache_write_copy = "cache-write reporting capability unknown"
        elif cache_write_reporting == "partial":
            cache_write_copy = f"{_fmt_compact_int(cache_write)} reported cache writes (partial coverage)"
        else:
            cache_write_copy = f"{_fmt_compact_int(cache_write)} cache writes"
        if cache_read_reporting == "not_reported":
            cache_read_copy = "cache reads not reported by this source"
        elif cache_read_reporting == "unknown":
            cache_read_copy = "cache-read reporting capability unknown"
        elif cache_read_reporting == "partial":
            cache_read_copy = f"{_fmt_compact_int(cache_read)} reported cache reads (partial coverage)"
        else:
            cache_read_copy = f"{_fmt_compact_int(cache_read)} cache reads"
        cache_split_copy = (
            "cache split partial; totals still include unseparated input"
            if cache_write_reporting in {"not_reported", "partial", "unknown"}
            or cache_read_reporting in {"not_reported", "partial", "unknown"}
            else "cache split complete"
        )
        normalization_copy = (
            f" · {_fmt_int(excluded_rows)} usage row(s) held for source identity or lineage normalization"
            if excluded_rows
            else ""
        )
        total_metric_label = "Known subtotal incl. cache" if excluded_rows else "Total incl. cache"
        main = f"""
        <div class="usage-pulse-main">
          <div class="usage-pulse-metrics">
            <div class="usage-pulse-metric"><strong>{esc(_fmt_compact_int(composition_total))}</strong><span>{esc(total_metric_label)}</span></div>
            <div class="usage-pulse-metric"><strong>{esc(cost_value)}</strong><span>{esc(cost_label)}</span></div>
            <div class="usage-pulse-metric"><strong>{esc(_fmt_compact_int(totals.get('sessions')))}</strong><span>Sessions</span></div>
            <div class="usage-pulse-metric"><strong>{esc(_fmt_compact_int(rows))}</strong><span>Usage rows</span></div>
          </div>
          <div class="usage-composition" role="img" aria-label="Token composition: {esc(_fmt_int(uncached_input))} input after reported cache, {esc(_fmt_int(output))} output, {esc(cache_write_copy)}, {esc(cache_read_copy)}">
            <span class="uncached-input" style="width:{share(uncached_input)}"></span><span class="output" style="width:{share(output)}"></span><span class="cache-write" style="width:{share(cache_write)}"></span><span class="cache-read" style="width:{share(cache_read)}"></span>
          </div>
          <p class="usage-composition-note">Breakdown · {esc(_fmt_compact_int(uncached_input))} input after reported cache · {esc(_fmt_compact_int(output))} output · {esc(cache_write_copy)} · {esc(cache_read_copy)} · {esc(cache_split_copy)} · {esc(confidence)} · {esc(coverage)}{esc(normalization_copy)}</p>
        </div>
        """
    supporting_copy = (
        f"{_fmt_int(main_runs)} root {'chat' if main_runs == 1 else 'chats'} · "
        f"{_fmt_int(supporting)} supporting {'session' if supporting == 1 else 'sessions'} collapsed."
        if supporting
        else f"{_fmt_int(main_runs)} root {'chat' if main_runs == 1 else 'chats'}."
    )
    if unresolved_groups:
        supporting_copy = supporting_copy.rstrip(".") + (
            f" · {_fmt_int(unresolved_groups)} unresolved lineage "
            f"{'group' if unresolved_groups == 1 else 'groups'}."
        )
    usage_truth_copy = (
        "Known additive token subtotal is shown; held rows remain raw evidence until source identity or lineage normalization."
        if excluded_rows
        else "Cache-inclusive totals preserve each source total; input, output, and reported cache are shown separately."
    )
    return f"""
    <section class="usage-pulse" aria-labelledby="usage-pulse-title">
      <div class="usage-pulse-copy">
        <div class="eyebrow">Saved usage · all history</div>
        <h2 id="usage-pulse-title">Workspace usage</h2>
        <p>{esc(supporting_copy)} {esc(usage_truth_copy)}</p>
        <a class="see-all" href="/tokens">Explore usage →</a>
      </div>
      {main}
    </section>
    """


def _overview_cost_value_and_label(totals: Mapping[str, Any]) -> tuple[str, str]:
    """Cost value + honest label for a usage-cube ``totals`` bucket. Never
    coerces an unknown cost to $0 and never labels a partial subtotal as a
    complete estimate."""

    additive_rows = int(totals.get("additive_rows") or 0)
    if not additive_rows:
        return "No estimate", "No priced usage"
    estimated = totals.get("estimated_cost_usd")
    unpriced = int(totals.get("unpriced_rows") or 0)
    if estimated is not None:
        return _fmt_usd(estimated), ("Partial est. cost" if unpriced else "Est. API cost")
    known = totals.get("known_additive_cost_usd")
    if known is not None:
        return _fmt_usd(known), "Known subtotal"
    return "No estimate", "Cost unavailable"


def _overview_metric_tiles_html(
    *,
    tracked_count: int,
    verified_count: int,
    action_count: int,
    usage_totals_30d: Mapping[str, Any],
    active_clients: list[str],
    esc: Any,
) -> str:
    """Four summary tiles: tracked tasks, 30-day tokens, 30-day est. cost, and
    active agents. Usage figures come from the scoped 30-day cube; task figures
    from the same projection the hero uses. Nothing is invented."""

    tokens = int(usage_totals_30d.get("total_tokens_including_cached") or 0)
    rows_30d = int(usage_totals_30d.get("rows") or 0)
    additive_30d = int(usage_totals_30d.get("additive_rows") or 0)
    held_30d = int(usage_totals_30d.get("excluded_non_additive_rows") or 0)
    cost_value, cost_label = _overview_cost_value_and_label(usage_totals_30d)
    verified_note = f"{_fmt_int(verified_count)} verified"
    if action_count:
        verified_note += f" · {_fmt_int(action_count)} need attention"
    # Never show a held-masked "0" as a real measured zero. When the window has
    # rows but none are additive (held-only), the token value is unknown, not
    # zero: mark it "—" and disclose the held count, mirroring the cost tile.
    if additive_30d:
        tokens_value = _fmt_compact_int(tokens)
        tokens_note = "incl. cache · last 30 days"
        if held_30d:
            tokens_note += f" · {_fmt_int(held_30d)} held"
    elif held_30d:
        tokens_value = "—"
        tokens_note = f"{_fmt_int(held_30d)} usage row(s) held · last 30 days"
    else:
        tokens_value = "0"
        tokens_note = "no saved rows in the last 30 days"
    agents_note = " · ".join(_human_client(client) for client in active_clients) or "No agents observed yet"
    return f"""
    <div class="metric-grid ov-metric-grid">
      <div class="metric"><div class="label">Tracked tasks</div><div class="value">{esc(_fmt_int(tracked_count))}</div><div class="note">{esc(verified_note)}</div></div>
      <div class="metric log"><div class="label">Tokens · 30d</div><div class="value">{esc(tokens_value)}</div><div class="note">{esc(tokens_note)}</div></div>
      <div class="metric cost"><div class="label">Est. cost · 30d</div><div class="value">{esc(cost_value)}</div><div class="note">{esc(cost_label)}</div></div>
      <div class="metric bridge"><div class="label">Active agents</div><div class="value">{esc(_fmt_int(len(active_clients)))}</div><div class="note">{esc(agents_note)}</div></div>
    </div>
    """


def _overview_usage_charts_html(
    *,
    line_html: str,
    bar_charts: dict[str, str],
    active_breakdown: str,
    range_label: str,
    measured_days: int,
    esc: Any,
) -> str:
    """Usage section: the cumulative line chart plus the daily stacked-bar chart
    with a CSS-only (no-JS) breakdown selector.

    All three breakdown charts (agent / model / agent-model) are rendered up
    front; visually-hidden radio inputs plus `:checked ~` sibling rules show one
    at a time, so switching costs no JS and no page navigation. The radios and
    panels are siblings under `.ov-usage-bars` on purpose — the general-sibling
    combinator only reaches following siblings. The `?usage_breakdown=` deep link
    still works: it sets which radio starts `checked`, so an existing link opens
    on the right tab and the user can switch from there without a reload.
    """

    labels = (("agent", "By agent"), ("model", "By model"), ("agent-model", "By agent-model"))
    radios: list[str] = []
    tabs: list[str] = []
    panels: list[str] = []
    for key, label in labels:
        rid = f"ov-bd-{key}"
        checked = " checked" if key == active_breakdown else ""
        current = ' aria-current="true"' if key == active_breakdown else ""
        radios.append(f'<input type="radio" name="ov-usage-breakdown" id="{rid}" class="ov-utab-radio"{checked}>')
        tabs.append(f'<label class="ov-utab" for="{rid}"{current}>{esc(label)}</label>')
        panels.append(f'<div class="ov-utab-panel ov-utab-panel-{key}">{bar_charts[key]}</div>')
    measured_note = (
        f"{_fmt_int(measured_days)} measured {'day' if measured_days == 1 else 'days'} in this window"
        if measured_days
        else "No measured days in this window yet"
    )
    return f"""
    <section class="section ov-usage" aria-labelledby="ov-usage-title">
      <div class="section-header"><div><div class="eyebrow">Usage · {esc(range_label)}</div><h2 id="ov-usage-title">Token usage over time</h2><p>Saved rows only. Held and undated rows are labeled, never summed silently. {esc(measured_note)}.</p></div><a class="see-all" href="/tokens">Explore usage →</a></div>
      <div class="ov-chart-block">
        <div class="ov-chart-subhead"><span>Total usage</span><span class="note">cumulative · {esc(range_label)}</span></div>
        {line_html}
      </div>
      <div class="ov-chart-block ov-usage-bars">
        {"".join(radios)}
        <div class="ov-chart-subhead"><span>Daily usage</span><span class="ov-utabs" role="tablist" aria-label="Daily usage breakdown">{"".join(tabs)}</span></div>
        {"".join(panels)}
      </div>
    </section>
    """


def _overview_client_usage_totals(session_groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-client token + cost aggregate over the already-scoped session groups
    (all history). Uses each group's own usage aggregate so the roster's tokens
    and cost describe exactly the sessions the roster counts. Held rows and
    unknown costs stay unknown, never zero."""

    per_client: dict[str, dict[str, Any]] = {}
    for group in session_groups:
        client = str(group.get("client") or "")
        usage = group.get("usage") if isinstance(group.get("usage"), Mapping) else {}
        agg = per_client.setdefault(
            client,
            {
                "total_tokens": 0,
                "known_cost": 0.0,
                "has_known_cost": False,
                "additive_rows": 0,
                "excluded_rows": 0,
                "unpriced_rows": 0,
                "all_complete": True,
            },
        )
        agg["total_tokens"] += int(usage.get("total_tokens") or 0)
        known = usage.get("known_additive_cost_usd")
        if known is not None:
            agg["known_cost"] += float(known)
            agg["has_known_cost"] = True
        agg["additive_rows"] += int(usage.get("additive_rows") or 0)
        agg["excluded_rows"] += int(usage.get("excluded_non_additive_rows") or 0)
        agg["unpriced_rows"] += int(usage.get("unpriced_rows") or 0)
        if not usage.get("cost_complete"):
            agg["all_complete"] = False
    return per_client


def _overview_client_usage_line(agg: Mapping[str, Any] | None) -> str:
    """The 'X tok · $Y' line for one roster card, honest about held/unknown."""

    if not agg or int(agg.get("additive_rows") or 0) <= 0:
        return '<span class="ov-agent-usage-empty">usage not reported</span>'
    tokens = int(agg.get("total_tokens") or 0)
    if agg.get("has_known_cost"):
        complete = bool(agg.get("all_complete")) and not int(agg.get("unpriced_rows") or 0) and not int(agg.get("excluded_rows") or 0)
        cost_html = f'<strong>{_fmt_usd(agg.get("known_cost"))}</strong>{"" if complete else " known"}'
    else:
        cost_html = '<strong>No estimate</strong>'
    return f'<span><strong>{_fmt_compact_int(tokens)}</strong> tok</span><span>{cost_html}</span>'


def _overview_top_sessions_html(
    session_groups: list[dict[str, Any]], esc: Any, *, limit: int = 5
) -> str:
    """Top root chats by estimated cost. Only sessions with a real priced cost
    are ranked; unpriced/held sessions are not invented as $0 and simply do not
    appear here (the roster still counts them)."""

    ranked: list[tuple[float, dict[str, Any]]] = []
    for group in session_groups:
        usage = group.get("usage") if isinstance(group.get("usage"), Mapping) else {}
        estimated = usage.get("estimated_cost_usd")
        known = usage.get("known_additive_cost_usd")
        sort_cost = estimated if estimated is not None else known
        if sort_cost is None:
            continue
        ranked.append((float(sort_cost), group))
    if not ranked:
        return ""
    ranked.sort(key=lambda pair: -pair[0])
    rows: list[str] = []
    for _sort_cost, group in ranked[:limit]:
        root = group.get("root") if isinstance(group.get("root"), Mapping) else {}
        usage = group.get("usage") if isinstance(group.get("usage"), Mapping) else {}
        client = str(group.get("client") or "")
        work_titles = group.get("work_titles") if isinstance(group.get("work_titles"), list) else []
        # No session-id fragment on the product home: displaying ``ROOT[:8]`` is
        # a /sessions-only affordance (locked decision). Fall back to a work
        # title or a neutral label, never a raw id.
        title = (
            str(root.get("client_session_title") or "").strip()
            or (str(work_titles[0]).strip() if work_titles else "")
            or "Root chat"
        )
        estimated = usage.get("estimated_cost_usd")
        known = usage.get("known_additive_cost_usd")
        if estimated is not None:
            cost_text = _fmt_usd(estimated)
        elif known is not None:
            cost_text = f"{_fmt_usd(known)} known"
        else:
            cost_text = "No estimate"
        tokens = int(usage.get("total_tokens") or 0)
        lane_class = client_lane_class(client)
        rows.append(
            '<div class="ov-topsess-row">'
            f'<span class="agent-avatar {esc(lane_class)} is-mini" aria-hidden="true">{esc(_agent_mark(client))}</span>'
            f'<div class="ov-topsess-copy"><strong>{esc(_short_text(title, max_length=54))}</strong>'
            f'<span>{esc(_human_client(client))}</span></div>'
            f'<div class="ov-topsess-cost"><strong>{esc(cost_text)}</strong>'
            f'<span>{esc(_fmt_compact_int(tokens))} tok</span></div>'
            "</div>"
        )
    return (
        '<div class="ov-topsess">'
        '<div class="ov-topsess-head">Top root chats by est. cost</div>'
        + "".join(rows)
        + "</div>"
    )


def _agent_roster_html(
    session_groups: list[dict[str, Any]],
    esc: Any,
    *,
    usage_by_client: Mapping[str, Mapping[str, Any]] | None = None,
    top_sessions_html: str = "",
) -> str:
    grouped: dict[str, dict[str, Any]] = {}
    for session_group in session_groups:
        client = str(session_group.get("client") or "").strip()
        group = grouped.setdefault(client, {"runs": 0, "unresolved": 0, "supporting": 0, "models": []})
        if str(session_group.get("lineage_state") or "resolved_root") == "resolved_root":
            group["runs"] += 1
        else:
            group["unresolved"] += 1
        group["supporting"] += int(session_group.get("supporting_count") or 0)
        for model in list(session_group.get("models") or []):
            if model not in group["models"]:
                group["models"].append(model)
    ordered = sorted(
        grouped.items(),
        key=lambda pair: (-(int(pair[1]["runs"]) + int(pair[1]["unresolved"])), _human_client(pair[0])),
    )
    if not ordered:
        roster = '<div class="agent-board-empty">No saved session identity is available for this workspace yet.</div>'
    else:
        max_count = max(int(group["runs"]) + int(group["unresolved"]) for _client, group in ordered)
        cards = []
        for client, group in ordered:
            count = int(group["runs"])
            unresolved_count = int(group["unresolved"])
            support_count = int(group["supporting"])
            group_count = count + unresolved_count
            width = max(6, round(group_count / max_count * 100))
            lane_class = client_lane_class(client)
            run_copy = f"{_fmt_int(count)} root {'chat' if count == 1 else 'chats'}"
            if unresolved_count:
                run_copy += (
                    f" · {_fmt_int(unresolved_count)} unresolved lineage "
                    f"{'group' if unresolved_count == 1 else 'groups'}"
                )
            if support_count:
                run_copy += (
                    f" · {_fmt_int(support_count)} supporting "
                    f"{'session' if support_count == 1 else 'sessions'}"
                )
            client_usage = (usage_by_client or {}).get(client)
            cards.append(
                '<article class="agent-roster-item">'
                '<div class="agent-roster-head">'
                f'<span class="agent-avatar {esc(lane_class)}" aria-hidden="true">{esc(_agent_mark(client))}</span>'
                f'<div class="agent-roster-name"><strong>{esc(_human_client(client))}</strong><span>{esc(run_copy)}</span></div>'
                '</div>'
                f'<div class="agent-models">{_model_chips_html(list(group["models"]), esc, limit=3, unknown_label="Model unavailable")}</div>'
                f'<div class="agent-nums">{_overview_client_usage_line(client_usage)}</div>'
                f'<div class="agent-volume" role="img" aria-label="{esc(_human_client(client))}: {esc(_fmt_int(group_count))} top-level {"group" if group_count == 1 else "groups"}"><span class="{esc(lane_class)}" style="width:{width}%"></span></div>'
                '</article>'
            )
        roster = '<div class="agent-roster">' + "".join(cards) + "</div>"
    supporting_total = sum(int(group.get("supporting_count") or 0) for group in session_groups)
    main_total = sum(
        1 for group in session_groups if str(group.get("lineage_state") or "resolved_root") == "resolved_root"
    )
    unresolved_total = len(session_groups) - main_total
    board_copy = f"{_fmt_int(main_total)} root {'chat' if main_total == 1 else 'chats'}"
    if unresolved_total:
        board_copy += (
            f" · {_fmt_int(unresolved_total)} unresolved lineage "
            f"{'group' if unresolved_total == 1 else 'groups'}"
        )
    if supporting_total:
        board_copy += (
            f" · {_fmt_int(supporting_total)} supporting "
            f"{'session' if supporting_total == 1 else 'sessions'} collapsed"
        )
    return f"""
    <section class="agent-board" aria-labelledby="agent-board-title">
      <div class="agent-board-copy">
        <div class="eyebrow">Observed identity</div>
        <h2 id="agent-board-title">Agents &amp; models</h2>
        <p><strong>{esc(board_copy)}</strong>. Bars compare root chats, not internal session volume or live connection health.</p>
      </div>
      <div class="agent-board-main">
        {roster}
        {top_sessions_html}
      </div>
    </section>
    """


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

    def append_unique(rows: list[dict[str, Any]], event: Mapping[str, Any]) -> None:
        key = _evidence_event_key(event)
        if all(_evidence_event_key(row) != key for row in rows):
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
                for fact in namespace_facts_by_task[task_id]
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
            episodes.append(
                {
                    "target_digest": target_digest,
                    "finding_token": token,
                    "objective_state": "current_failure",
                    "disposition_state": disposition.state,
                    "attention_open": disposition.state == "open",
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
        "current_finding_count": len(all_episodes),
        "reviewed_finding_count": reviewed_count,
        "resolved_finding_count": resolved_count,
    }
    projection["finding_disposition_diagnostics"] = disposition_projection.diagnostics
    return projection


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
    return data.task_identity.decorate_projection(projection) if data.task_identity is not None else projection


def _legacy_overview_body(data: _DashboardPageData, evidence_product: Mapping[str, Any] | None = None) -> str:
    """Root-run dashboard with supporting sessions collapsed transitively."""

    _ = evidence_product  # Forensic Evidence-v2 projections remain in Advanced.
    esc = data.esc
    scoped_work = [
        item
        for item in data.work_items
        if isinstance(item, dict)
        and _overview_project_in_scope(data, item.get("project_dir"), item.get("project_identity"))
    ]
    identity_sessions = [
        entry
        for entry in data.rollup_sessions
        if isinstance(entry, dict)
        and _overview_project_in_scope(
            data,
            entry.get("project"),
            entry.get("project_identity"),
            entry.get("project_identity_state"),
        )
    ]
    session_groups = _overview_root_session_groups(identity_sessions)
    session_index = {
        (str(entry.get("client") or ""), str(entry.get("client_session_id") or "")): entry
        for entry in identity_sessions
        if isinstance(entry, dict)
    }
    session_group_by_member: dict[tuple[str, str], dict[str, Any]] = {}
    for session_group in session_groups:
        for member_key in session_group["member_keys"]:
            session_group_by_member[member_key] = session_group

    grouped_work: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in scoped_work:
        session_key = (
            str(item.get("client") or item.get("reporting_source") or ""),
            str(item.get("client_session_id") or ""),
        )
        session_group = session_group_by_member.get(session_key) if session_key[1] else None
        if _work_run_identity(item) is not None:
            # A reported run is the semantic product boundary. Multiple
            # logical runs may share one long-lived client root session.
            group_key = _work_group_key(item)
        elif session_group is not None:
            root_key = _session_identity_key(session_group["root"])
            group_key = ("session-root", root_key[0], root_key[1])
        else:
            group_key = _work_group_key(item)
        grouped_work.setdefault(group_key, []).append(item)

    feed_entries: list[dict[str, Any]] = []
    work_session_keys: set[tuple[str, str]] = set()
    states: list[dict[str, Any]] = []
    for items in grouped_work.values():
        state, state_item, title_item = _work_group_selection(items)
        states.append(state)
        is_reported_run = any(_work_run_identity(item) is not None for item in items)
        exact_sessions = _work_group_sessions(items, session_index)
        matched_groups = []
        seen_roots: set[tuple[str, str]] = set()
        for session in exact_sessions:
            session_group = session_group_by_member.get(_session_identity_key(session))
            if session_group is None:
                continue
            root_key = _session_identity_key(session_group["root"])
            if root_key not in seen_roots:
                matched_groups.append(session_group)
                seen_roots.add(root_key)
        if not is_reported_run and len(matched_groups) == 1:
            sessions = list(matched_groups[0]["members"])
            work_session_keys.update(matched_groups[0]["member_keys"])
        else:
            # Reported runs may share one long-lived root. Keep only exact
            # linked sessions for model identity and never repeat root totals
            # across each semantic run card. Mark the exact identity consumed
            # so the same lineage does not also appear as a raw activity card;
            # the workspace usage pulse still carries the complete total.
            sessions = exact_sessions
            work_session_keys.update(_session_identity_key(session) for session in exact_sessions)
        feed_entries.append(
            {
                "kind": "work_group",
                "items": items,
                "sessions": sessions,
                "state_item": state_item,
                "title_item": title_item,
                "state": state,
                "show_session_totals": not is_reported_run,
                "rank": int(state.get("rank") or 0),
                "updated_at": max(float(item.get("updated_at") or 0.0) for item in items),
            }
        )

    for session_group in session_groups:
        activity_group = _overview_residual_session_group(session_group, work_session_keys)
        if activity_group is None:
            continue
        feed_entries.append(
            {
                "kind": "activity_group",
                "group": activity_group,
                "rank": 4,
                "updated_at": float(activity_group.get("last_activity_at") or 0.0),
            }
        )

    # Explicit blockers stay first, active work follows, then agent findings.
    # Findings remain visible without being misclassified as user assignments.
    feed_entries.sort(
        key=lambda entry: (
            0
            if isinstance(entry.get("state"), Mapping) and entry["state"].get("action_required")
            else 1
            if isinstance(entry.get("state"), Mapping) and entry["state"].get("key") == "in_progress"
            else 2,
            0
            if isinstance(entry.get("state"), Mapping) and entry["state"].get("finding_open")
            else 1,
            -float(entry.get("updated_at") or 0.0),
        )
    )
    supporting_review_entries = [entry for entry in feed_entries if _is_collapsible_supporting_review(entry)]
    primary_entries = [entry for entry in feed_entries if not _is_collapsible_supporting_review(entry)]
    visible_entries = _visible_attention_entries(primary_entries)
    feed_html = "".join(
        _work_group_feed_item_html(
            entry["items"],
            state=entry["state"],
            state_item=entry["state_item"],
            title_item=entry["title_item"],
            sessions=entry["sessions"],
            show_session_totals=entry["show_session_totals"],
            csrf_token=data.task_csrf_token,
            esc=esc,
        )
        if entry["kind"] == "work_group"
        else _root_activity_feed_item_html(entry["group"], esc=esc)
        for entry in visible_entries
    )
    feed_html += _supporting_review_rollup_html(supporting_review_entries, esc)
    if not feed_html:
        feed_html = (
            '<div class="empty-state">No work or local activity has been recorded for this workspace yet. '
            "Start an agent with agentacct recording enabled, or refresh to import local activity.</div>"
        )

    in_progress_count = sum(1 for state in states if state.get("key") == "in_progress")
    action_count = sum(1 for state in states if state.get("action_required"))
    finding_count = sum(1 for state in states if state.get("finding_open"))
    verified_count = sum(1 for state in states if state.get("key") == "verified")
    if action_count:
        headline = f"{_fmt_int(action_count)} {'run needs' if action_count == 1 else 'runs need'} input"
        summary = "Only explicit blockers are counted here. Agent findings remain separate from agentacct health and user assignments."
    elif in_progress_count:
        headline = f"{_fmt_int(in_progress_count)} {'run' if in_progress_count == 1 else 'runs'} in progress"
        summary = "Active work comes first. Agent findings remain visible as evidence about the work, not agentacct failures."
    elif finding_count:
        headline = (
            f"1 run has an open finding"
            if finding_count == 1
            else f"{_fmt_int(finding_count)} runs have open findings"
        )
        summary = "Agents found issues in the work being reviewed. agentacct recorded them without assigning the next action to you."
    else:
        headline = "You're caught up"
        summary = "Nothing recorded needs your input or has an open finding. Recent outcomes and observed activity are below."
    verified_share = round(verified_count / len(states) * 100) if states else 0
    if supporting_review_entries:
        primary_note = (
            f"Showing {len(visible_entries)} of {len(primary_entries)} primary runs"
            if len(visible_entries) < len(primary_entries)
            else f"{len(primary_entries)} primary {'run' if len(primary_entries) == 1 else 'runs'}"
        )
        cap_note = (
            f"{primary_note} · {len(supporting_review_entries)} completed supporting "
            f"{'review' if len(supporting_review_entries) == 1 else 'reviews'} collapsed."
        )
    else:
        cap_note = (
            f"Showing {len(visible_entries)} of {len(primary_entries)} recent runs."
            if len(visible_entries) < len(primary_entries)
            else f"{len(primary_entries)} recent {'run' if len(primary_entries) == 1 else 'runs'} in this workspace."
        )

    return f"""
    <section class="work-overview" aria-labelledby="work-overview-title">
      <div class="work-overview-copy"><div class="eyebrow">Workspace pulse</div><h2 id="work-overview-title">{esc(headline)}</h2><p>{esc(summary)}</p></div>
      <div class="work-overview-stats">
        <div class="outcome-ring" style="--verified-share:{verified_share}%" role="img" aria-label="{esc(_fmt_int(verified_count))} of {esc(_fmt_int(len(states)))} run groups verified"><div class="outcome-ring-label"><strong>{esc(_fmt_int(verified_count))}</strong><span>Verified</span></div></div>
        <div class="work-overview-stat-stack">
          <div class="work-overview-stat"><strong>{esc(_fmt_int(in_progress_count))}</strong><span>In progress</span></div>
          <div class="work-overview-stat finding"><strong>{esc(_fmt_int(finding_count))}</strong><span>Open findings</span></div>
          <div class="work-overview-stat action"><strong>{esc(_fmt_int(action_count))}</strong><span>Needs input</span></div>
        </div>
      </div>
    </section>

    {_overview_usage_pulse_html(_overview_usage_totals(data, identity_sessions), session_groups, esc)}

    {_agent_roster_html(session_groups, esc)}

    <section class="section run-section" id="work-feed" aria-label="Work feed">
      <div class="section-header"><div><div class="eyebrow">Run timeline</div><h2>Recent agent runs</h2><p>Root runs, grouped work, execution signals, and outcome — supporting sessions stay collapsed.</p></div><a class="see-all" href="/sessions">View all activity →</a></div>
      <div class="work-feed">{feed_html}</div>
      <div class="work-feed-footer"><p>{esc(cap_note)}</p><div><a class="see-all" href="/tokens">View usage →</a> · <a class="see-all" href="/advanced">Data &amp; evidence →</a></div></div>
    </section>
    """


def _activation_panel_html(payload: Mapping[str, Any] | None, esc: Any) -> str:
    if not isinstance(payload, Mapping) or payload.get("stage") == "active":
        return ""
    stage = str(payload.get("stage") or "configuration_needed")
    descriptions = {
        "client_needed": "agentacct runs locally. First, let it find one known coding-agent source on this machine.",
        "configuration_needed": "Your local agent activity is readable. Finish one project-local setup so future Tasks include semantic work context.",
        "new_session_needed": "The project is connected. Open a new agent chat so the client loads agentacct's MCP server and hooks.",
        "runtime_needed": "Capture is configured. Start the managed local runtime to keep activity synced without babysitting the preview.",
        "waiting_for_task": "Everything is connected. Run one real Task in your agent; agentacct will replace this setup card with the Task automatically.",
    }
    progress = payload.get("progress") if isinstance(payload.get("progress"), list) else []
    steps = []
    for row in progress:
        if not isinstance(row, Mapping):
            continue
        state = str(row.get("state") or "unknown")
        css = state.replace("_", "-")
        detail = str(row.get("detail") or state.replace("_", " "))
        steps.append(
            f'<div class="activation-step is-{esc(css)}"><strong>{esc(row.get("label") or row.get("key") or "Step")}</strong>'
            f'<span>{esc(detail)}</span></div>'
        )
    action = payload.get("primary_action") if isinstance(payload.get("primary_action"), Mapping) else {}
    command = str(action.get("command") or "")
    command_html = f'<code class="activation-command">{esc(command)}</code>' if command else ""
    clients = [str(client) for client in payload.get("detected_clients", []) if client]
    clients_copy = (
        "Detected: " + ", ".join(_human_client(client) for client in clients) + ". "
        if clients
        else ""
    )
    return f"""
    <section class="activation-card" aria-labelledby="activation-title">
      <div class="activation-head"><div class="eyebrow">Get to your first real Task</div><h2 id="activation-title">{esc(payload.get("headline") or "Connect agentacct")}</h2><p>{esc(descriptions.get(stage, "agentacct is checking the local capture path."))}</p></div>
      <div class="activation-steps" aria-label="Activation progress">{"".join(steps)}</div>
      <div class="activation-action"><div class="activation-action-copy"><strong>{esc(action.get("label") or "Continue setup")}</strong><span>{esc(action.get("reason") or "Complete the next evidence-backed step.")}</span></div>{command_html}</div>
      <p class="activation-permission">{esc(clients_copy)}Local only: agentacct reads local agent activity and writes project-local config/state. It does not request provider API keys.</p>
    </section>
    """


def _overview_body(
    data: _DashboardPageData,
    evidence_product: Mapping[str, Any] | None = None,
    activation: Mapping[str, Any] | None = None,
    *,
    usage_breakdown: str = "agent",
) -> str:
    """Session-first Task home; run ids and work sections stay nested."""

    _ = evidence_product
    esc = data.esc
    projection = _dashboard_task_projection(data)

    def task_in_scope(task: Mapping[str, Any]) -> bool:
        sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
        if any(
            isinstance(session, Mapping)
            and _overview_project_in_scope(
                data,
                session.get("project"),
                session.get("project_identity"),
                session.get("project_identity_state"),
            )
            for session in sessions
        ):
            return True
        items = task.get("work_items") if isinstance(task.get("work_items"), list) else []
        return any(
            isinstance(item, Mapping)
            and _overview_project_in_scope(data, item.get("project_dir"), item.get("project_identity"))
            for item in items
        )

    tasks = [
        task
        for task in (projection.get("tasks") if isinstance(projection.get("tasks"), list) else [])
        if isinstance(task, dict) and task_in_scope(task)
    ]
    # Root session groups (all in-scope sessions, incl. ones with no recorded
    # work) are needed both for the activity feed below and, later, for the
    # usage charts / roster. Compute once here.
    scoped_sessions = [
        entry
        for entry in data.rollup_sessions
        if isinstance(entry, dict)
        and _overview_project_in_scope(
            data,
            entry.get("project"),
            entry.get("project_identity"),
            entry.get("project_identity_state"),
        )
    ]
    session_groups = _overview_root_session_groups(scoped_sessions)
    task_entries: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    for task in tasks:
        state, _state_item = _task_product_state(task)
        states.append(state)
        task_entries.append(
            {
                "kind": "task",
                "task": task,
                "state": state,
                "updated_at": max(
                    float(task.get("last_activity_at") or 0.0),
                    max(
                        (
                            float(item.get("updated_at") or 0.0)
                            for item in task.get("work_items", [])
                            if isinstance(item, Mapping)
                        ),
                        default=0.0,
                    ),
                ),
            }
        )

    unresolved_items = [
        unresolved.get("item")
        for unresolved in (
            projection.get("unresolved_work")
            if isinstance(projection.get("unresolved_work"), list)
            else []
        )
        if isinstance(unresolved, Mapping)
        and isinstance(unresolved.get("item"), dict)
        and _overview_project_in_scope(
            data,
            unresolved["item"].get("project_dir"),
            unresolved["item"].get("project_identity"),
        )
    ]
    grouped_unresolved: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in unresolved_items:
        grouped_unresolved.setdefault(_work_group_key(item), []).append(item)
    for items in grouped_unresolved.values():
        state, state_item, title_item = _work_group_selection(items)
        states.append(state)
        task_entries.append(
            {
                "kind": "unlinked",
                "items": items,
                "state": state,
                "state_item": state_item,
                "title_item": title_item,
                "updated_at": max(float(item.get("updated_at") or 0.0) for item in items),
            }
        )

    unassigned_findings = [
        finding
        for finding in (
            projection.get("unassigned_findings")
            if isinstance(projection.get("unassigned_findings"), list)
            else []
        )
        if isinstance(finding, Mapping)
        and isinstance(finding.get("event"), Mapping)
        and _overview_project_in_scope(
            data,
            finding["event"].get("project_dir"),
            finding["event"].get("project_identity"),
        )
    ]
    unassigned_findings.sort(
        key=lambda finding: -float(finding.get("updated_at") or 0.0)
    )
    # Open findings are actionable attention state and must never disappear
    # behind the ordinary recent-content cap.
    visible_unassigned_findings = unassigned_findings
    unassigned_findings_html = "".join(
        _unassigned_finding_feed_item_html(
            finding,
            csrf_token=data.task_csrf_token,
            esc=esc,
        )
        for finding in visible_unassigned_findings
    )
    unassigned_findings_section = ""
    if unassigned_findings_html:
        finding_cap = (
            f"Showing {len(visible_unassigned_findings)} of {len(unassigned_findings)} unassigned findings."
            if len(visible_unassigned_findings) < len(unassigned_findings)
            else (
                f"{len(unassigned_findings)} unassigned "
                f"{'finding' if len(unassigned_findings) == 1 else 'findings'}."
            )
        )
        unassigned_findings_section = f"""
    <section class="section run-section workspace-findings" id="workspace-findings" aria-label="Workspace findings">
      <div class="section-header"><div><div class="eyebrow">Unassigned evidence</div><h2>Workspace findings</h2><p>These failed checks could not be assigned to one Task without guessing. They remain visible and never count as Needs input.</p></div></div>
      <div class="work-feed">{unassigned_findings_html}</div>
      <div class="work-feed-footer"><p>{esc(finding_cap)}</p><div><a class="see-all" href="/advanced">Inspect evidence →</a></div></div>
    </section>
        """

    disposed_unassigned_findings = [
        finding
        for finding in (
            projection.get("disposed_unassigned_findings")
            if isinstance(projection.get("disposed_unassigned_findings"), list)
            else []
        )
        if isinstance(finding, Mapping)
        and isinstance(finding.get("event"), Mapping)
        and _overview_project_in_scope(
            data,
            finding["event"].get("project_dir"),
            finding["event"].get("project_identity"),
        )
    ]
    disposed_unassigned_findings.sort(
        key=lambda finding: -float(finding.get("updated_at") or 0.0)
    )
    disposed_unassigned_html = "".join(
        _unassigned_finding_feed_item_html(
            finding,
            csrf_token=data.task_csrf_token,
            esc=esc,
        )
        for finding in disposed_unassigned_findings
    )
    disposed_unassigned_section = ""
    if disposed_unassigned_html:
        disposed_count = len(disposed_unassigned_findings)
        disposed_unassigned_section = f"""
    <section class="section run-section workspace-findings finding-history" id="workspace-finding-history" aria-label="Reviewed workspace findings">
      <details>
        <summary>{esc(_fmt_int(disposed_count))} reviewed workspace {'finding' if disposed_count == 1 else 'findings'}</summary>
        <p class="section-note">These checks still have failed/error evidence, but you reviewed or marked their attention state resolved. Reopen any item if it needs attention again.</p>
        <div class="work-feed">{disposed_unassigned_html}</div>
      </details>
    </section>
        """

    # Time-first: newest activity on top. The task projection already emits one
    # entry per root session — work-bearing OR usage-only — so a recent hands-on
    # session (with no recorded work yet) is a first-class entry here, not hidden.
    # Attribution / severity are surfaced as card badges and the pinned
    # Needs-attention strip below, never as the sort key.
    task_entries.sort(key=lambda entry: -float(entry.get("updated_at") or 0.0))

    def _entry_is_attention(entry: Mapping[str, Any]) -> bool:
        # Open findings and blockers are pinned so they are never capped out of
        # the recent slice, matching the pre-time-first force-keep behaviour.
        state = entry.get("state")
        return isinstance(state, Mapping) and bool(
            state.get("action_required") or state.get("finding_open")
        )

    def _render_feed_entry(entry: Mapping[str, Any]) -> str:
        if entry["kind"] == "task":
            return _task_feed_item_html(
                entry["task"],
                candidate_tasks=[candidate for candidate in tasks if candidate is not entry["task"]],
                csrf_token=data.task_csrf_token,
                esc=esc,
            )
        return _work_group_feed_item_html(
            entry["items"],
            state=entry["state"],
            state_item=entry["state_item"],
            title_item=entry["title_item"],
            sessions=[],
            show_session_totals=False,
            csrf_token=data.task_csrf_token,
            esc=esc,
        )

    # Actionable entries (open findings, blockers) are pinned in a Needs-attention
    # strip above the chronological feed; the recent feed below is a pure
    # newest-first slice of everything else.
    attention_entries = [entry for entry in task_entries if _entry_is_attention(entry)]
    recent_entries = [entry for entry in task_entries if not _entry_is_attention(entry)]
    visible_attention = attention_entries[:DASHBOARD_ATTENTION_LIMIT]
    visible_recent = recent_entries[:DASHBOARD_RECENT_ACTIVITY_LIMIT]
    attention_feed_html = "".join(_render_feed_entry(entry) for entry in visible_attention)
    feed_html = "".join(_render_feed_entry(entry) for entry in visible_recent)
    if not feed_html and not attention_feed_html:
        feed_html = (
            '<div class="empty-state">No work or local activity has been recorded for this workspace yet. '
            "Refresh local activity or start an agent with agentacct recording enabled.</div>"
        )

    in_progress_count = sum(1 for state in states if state.get("key") == "in_progress")
    action_count = sum(1 for state in states if state.get("action_required"))
    task_finding_count = sum(
        len(task.get("open_finding_events") or [])
        for task in tasks
    )
    unresolved_finding_count = sum(
        len(item.get("open_finding_events") or [])
        for item in unresolved_items
    )
    assigned_finding_count = task_finding_count + unresolved_finding_count
    finding_count = assigned_finding_count + len(unassigned_findings)
    verified_count = sum(1 for state in states if state.get("key") == "verified")
    if action_count:
        headline = f"{_fmt_int(action_count)} {'task needs' if action_count == 1 else 'tasks need'} input"
        summary = "Only explicit blockers are counted here. Agent findings remain separate from agentacct health and user assignments."
    elif in_progress_count:
        headline = f"{_fmt_int(in_progress_count)} {'task' if in_progress_count == 1 else 'tasks'} in progress"
        summary = "Active Tasks come first. Agent findings remain visible as evidence about the work, not agentacct failures."
    elif finding_count:
        if not unassigned_findings and finding_count == 1:
            headline = (
                "1 task has an open finding"
            )
        elif len(unassigned_findings) == finding_count:
            headline = (
                "1 unassigned agent finding"
                if finding_count == 1
                else f"{_fmt_int(finding_count)} unassigned agent findings"
            )
        else:
            headline = (
                "1 open finding"
                if finding_count == 1
                else f"{_fmt_int(finding_count)} open findings"
            )
        summary = "Agents found issues in the work being reviewed. agentacct recorded them without assigning the next action to you."
    else:
        headline = "You're caught up"
        summary = "Nothing recorded needs your input or has an open finding. Recent activity is below, newest first."
    verified_share = round(verified_count / len(states) * 100) if states else 0
    cap_note = (
        f"Showing the {len(visible_recent)} most recent of {len(recent_entries)} activity entries."
        if len(visible_recent) < len(recent_entries)
        else f"{len(recent_entries)} recent {'entry' if len(recent_entries) == 1 else 'entries'} in this workspace."
    )

    # Dashboard v2 usage surface. One scoped 30-day daily cube drives the metric
    # tiles, the cumulative line, and the daily stacked bars so they can never
    # describe a different population or window. The all-history usage pulse is
    # unchanged (its own tests pin it); the charts sit in their own section so no
    # chart value can leak into the pulse's slice.
    today = date.today()
    safe_breakdown = _safe_choice(usage_breakdown, {"agent", "model", "agent-model"}, "agent")
    scoped_records = _overview_scoped_usage_records(data, scoped_sessions)
    usage_cube_30d = build_usage_cube(
        scoped_records,
        record_time=_usage_record_time,
        days=30,
        granularity="daily",
        today=today,
    )
    usage_totals_30d = usage_cube_30d["totals"]
    held_30d = int(usage_totals_30d.get("excluded_non_additive_rows") or 0)
    range_label = "last 30 days"
    breakdown_words = {"agent": "by agent", "model": "by model", "agent-model": "by agent-model"}
    # Render all three breakdowns up front so the tab selector can be CSS-only.
    # The cumulative line + measured-day count are breakdown-independent, so take
    # them from the first computed breakdown.
    line_days: list[tuple[str, int, int]] | None = None
    bar_charts: dict[str, str] = {}
    for key in ("agent", "model", "agent-model"):
        b_line_days, bar_days, breakdown_series = _overview_breakdown_series(
            usage_cube_30d, scoped_records, breakdown=key, today=today
        )
        if line_days is None:
            line_days = b_line_days
        bar_charts[key] = _overview_daily_stack_html(
            bar_days,
            breakdown_series,
            esc=esc,
            chart_id=f"ov-usage-bars-{key}",
            held_rows=held_30d,
            range_label=range_label,
            breakdown_label=breakdown_words[key],
        )
    assert line_days is not None
    measured_days = sum(1 for _period, rows, _total in line_days if rows > 0)
    usage_charts_html = _overview_usage_charts_html(
        line_html=_overview_usage_line_html(
            line_days,
            esc=esc,
            chart_id="ov-usage-line",
            held_rows=held_30d,
            range_label=range_label,
            measured_days=measured_days,
        ),
        bar_charts=bar_charts,
        active_breakdown=safe_breakdown,
        range_label=range_label,
        measured_days=measured_days,
        esc=esc,
    )

    client_group_counts: dict[str, int] = {}
    for group in session_groups:
        group_client = str(group.get("client") or "")
        if group_client:
            client_group_counts[group_client] = client_group_counts.get(group_client, 0) + 1
    active_clients = [
        client
        for client, _count in sorted(
            client_group_counts.items(), key=lambda pair: (-pair[1], _human_client(pair[0]))
        )
    ]
    metric_tiles_html = _overview_metric_tiles_html(
        tracked_count=len(task_entries),
        verified_count=verified_count,
        action_count=action_count,
        usage_totals_30d=usage_totals_30d,
        active_clients=active_clients,
        esc=esc,
    )
    roster_html = _agent_roster_html(
        session_groups,
        esc,
        usage_by_client=_overview_client_usage_totals(session_groups),
        top_sessions_html=_overview_top_sessions_html(session_groups, esc),
    )

    needs_attention_section = ""
    if attention_feed_html:
        attention_cap_note = ""
        if len(visible_attention) < len(attention_entries):
            attention_cap_note = (
                '<div class="work-feed-footer"><p>'
                f"{esc(f'Showing the {len(visible_attention)} newest of {len(attention_entries)} open items.')}"
                '</p><div><a class="see-all" href="/advanced">Review all findings →</a></div></div>'
            )
        needs_attention_section = f"""
    <section class="section run-section needs-attention" id="needs-attention" aria-label="Needs attention">
      <div class="section-header"><div><div class="eyebrow">Needs attention</div><h2>Open findings &amp; blockers</h2><p>The newest open findings and blockers are pinned here so they never scroll away with the recent-activity timeline below.</p></div></div>
      <div class="work-feed">{attention_feed_html}</div>
      {attention_cap_note}
    </section>
        """

    return f"""
    {_activation_panel_html(activation, esc)}
    <section class="work-overview" aria-labelledby="work-overview-title">
      <div class="work-overview-copy"><div class="eyebrow">Workspace pulse</div><h2 id="work-overview-title">{esc(headline)}</h2><p>{esc(summary)}</p></div>
      <div class="work-overview-stats">
        <div class="outcome-ring" style="--verified-share:{verified_share}%" role="img" aria-label="{esc(_fmt_int(verified_count))} of {esc(_fmt_int(len(states)))} Tasks verified"><div class="outcome-ring-label"><strong>{esc(_fmt_int(verified_count))}</strong><span>Verified</span></div></div>
        <div class="work-overview-stat-stack">
          <div class="work-overview-stat"><strong>{esc(_fmt_int(in_progress_count))}</strong><span>In progress</span></div>
          <div class="work-overview-stat finding"><strong>{esc(_fmt_int(finding_count))}</strong><span>Open findings</span></div>
          <div class="work-overview-stat action"><strong>{esc(_fmt_int(action_count))}</strong><span>Needs input</span></div>
        </div>
      </div>
    </section>

    {metric_tiles_html}

    {_overview_usage_pulse_html(_overview_usage_totals(data, scoped_sessions), session_groups, esc)}

    {usage_charts_html}

    {roster_html}

    {needs_attention_section}

    {unassigned_findings_section}

    <section class="section run-section" id="work-feed" aria-label="Recent activity">
      <div class="section-header"><div><div class="eyebrow">Activity timeline</div><h2>Recent activity</h2><p>Every recent session, newest first — chats you ran, with reported runs, work steps, and checks nested inside the ones that recorded work.</p></div><a class="see-all" href="/sessions">View all activity →</a></div>
      <div class="work-feed">{feed_html}</div>
      <div class="work-feed-footer"><p>{esc(cap_note)}</p><div><a class="see-all" href="/tokens">View usage →</a> · <a class="see-all" href="/advanced">Data &amp; evidence →</a></div></div>
    </section>

    {disposed_unassigned_section}
    """


def _tokens_body(
    data: _DashboardPageData,
    *,
    client: str = "all",
    model: str = "all",
    days: str = "30",
    granularity: str = "auto",
    cost_sort: str = "total",
) -> str:
    """Tokens `/tokens` (PRD §5.2): the Theme A explorer. Filter pill rows
    (every combination a URL, defaults all/all/30/auto), the stacked SVG
    chart, then the By platform / By model / By period tables — all over the
    SAME filtered saved-row population (one filter rule,
    usage_cube.filter_usage_records). The store-wide confidence tables stay
    at the bottom.

    Honesty (PRD §5.4): total reported tokens and estimated cost lead; input,
    output, cache-write, and cache-read categories remain visible as the
    breakdown. An unknown ``model`` value renders the EMPTY result with the
    filter echoed, never a guess (locked decision)."""

    esc = data.esc
    saved_records = data.usage_view.saved_records
    excluded_saved_records = data.usage_view.excluded_saved_records
    cube_records = [*saved_records, *excluded_saved_records]
    client = _safe_choice(client, {"all", *KNOWN_USAGE_CLIENTS}, "all")
    days = _safe_choice(days, set(USAGE_CUBE_DAYS_CHOICES), "30")
    granularity = _safe_choice(granularity, {"auto", *USAGE_CUBE_GRANULARITY_CHOICES}, "auto")
    cost_sort = _safe_choice(
        cost_sort,
        {"agent", "input", "output", "cache_create", "cache_read", "tokens", "total"},
        "total",
    )
    model = str(model or "all")
    models_present = models_in_records([*saved_records, *excluded_saved_records])
    model_known = model == "all" or model in models_present
    # An unknown model value is named ONCE in the note (escaped) and never
    # re-encoded into pill/sort URLs — a hostile-length value must not
    # multiply into every constructed href.
    url_model = model if model_known else "all"
    effective_granularity = resolve_granularity(days, granularity)
    client_filter = None if client == "all" else client
    model_filter = None if model == "all" else model
    days_filter = days_choice_to_int(days)
    # ONE today per request, passed to the cube (usage_cube never resolves
    # its own clock on a page path): a request served across local midnight
    # cannot render two different row populations. Every table on this page
    # (By platform / By model / By period / totals) renders this one cube.
    today = date.today()
    cube = build_usage_cube(
        cube_records,
        record_time=_usage_record_time,
        client=client_filter,
        model=model_filter,
        days=days_filter,
        granularity=effective_granularity,
        today=today,
    )
    excluded_in_range, _excluded_unknown_time = filter_usage_records(
        excluded_saved_records,
        record_time=_usage_record_time,
        client=client_filter,
        model=model_filter,
        days=days_filter,
        today=today,
    )
    all_time_cube = (
        build_usage_cube(
            cube_records,
            record_time=_usage_record_time,
            client=client_filter,
            model=model_filter,
            days=None,
            granularity="weekly",
            today=today,
        )
        if days != "all" and model_known
        else None
    )
    range_label = "all time" if days == "all" else f"last {days} days"

    # Model pills: top total-token models in the CURRENT range (capped with
    # an honest overflow note); the whitelist the filter validates against
    # stays all models ever saved.
    model_totals: dict[str, int] = {}
    for entry in cube["by_model"]:
        name = entry.get("model")
        if name:
            model_totals[str(name)] = model_totals.get(str(name), 0) + int(
                entry.get("total_tokens_including_cached") or 0
            )
    ranked_models = sorted(models_present, key=lambda name: (-model_totals.get(name, 0), name))
    pill_models, model_pill_note = _capped_value_pills(ranked_models, current=url_model, param="model")

    filter_controls = _tokens_filter_controls_html(
        client=client,
        model=model,
        url_model=url_model,
        days=days,
        granularity=granularity,
        effective_granularity=effective_granularity,
        pill_models=pill_models,
        model_pill_note=model_pill_note,
        cost_sort=cost_sort,
        esc=esc,
    )
    unknown_model_note = ""
    if not model_known:
        unknown_model_note = (
            f'<p class="section-note">Model <code>{esc(model)}</code> has no saved usage rows — showing the '
            "empty result for this filter (never a guess). Pick a model pill above.</p>"
        )
    preserved_history_note = _tokens_preserved_history_html(
        current_cube=cube,
        all_time_cube=all_time_cube,
        saved_records=cube_records,
        client=client,
        model=url_model,
        days=days,
        granularity=granularity,
        cost_sort=cost_sort,
        today=today,
        esc=esc,
    )
    replay_quarantine_note = ""
    if excluded_in_range:
        replay_quarantine_note = (
            '<p class="section-note"><strong>Usage normalization in progress.</strong> '
            f'{esc(_fmt_int(len(excluded_in_range)))} source-conflicted or lineage-dependent usage '
            f'{"row is" if len(excluded_in_range) == 1 else "rows are"} held out of token and cost totals in this range. '
            'Their raw counters remain in the ledger, but agentacct will not add them until source identity or lineage is proven.</p>'
        )
    totals_line = _tokens_filtered_totals_html(cube, days=days, esc=esc)
    chart_html = _tokens_chart_html(
        cube,
        esc=esc,
        chart_id="tokens-chart",
        granularity=effective_granularity,
        range_label=range_label,
        range_days=days_filter,
        today=today,
    )
    by_platform_rows = _tokens_by_platform_rows_html(cube, esc)
    by_model_rows, by_model_cap_note = _tokens_by_model_table_parts(cube, esc, cost_sort)
    model_sort_control = _sort_control(
        current=cost_sort,
        options=[
            ("total", "Est. cost"),
            ("tokens", "Total tokens"),
            ("input", "Input tokens"),
            ("output", "Output tokens"),
            ("cache_create", "Cache writes"),
            ("cache_read", "Cache reads"),
            ("agent", "Agent"),
        ],
        param="cost_sort",
        extra={"client": client, "model": url_model, "days": days, "granularity": granularity, "cost_sort": cost_sort},
        esc=esc,
        base="/tokens",
    )
    by_period_rows, by_period_cap_note = _tokens_by_period_table_parts(cube, esc, effective_granularity)
    cost_confidence_table_rows = _cost_confidence_table_html(saved_records, esc)
    usage_confidence_table_rows = _usage_confidence_table_html(saved_records, esc)

    return f"""    <section class="section" id="tokens-explorer">
      <div class="section-header"><h2>Tokens</h2><span class="note">Saved usage rows only ({esc(range_label)}) — imported logs are the usage truth; no live scan runs here.</span></div>
      <p class="section-note">{esc(COST_BASIS_DISCLOSURE)}</p>
      <p class="section-note">Total tokens and estimated cost lead. Breakdown columns keep input, output, cache writes, and cache reads visible. “Input after reported cache” is the remainder after counters the source actually supplied; if a cache counter is omitted, that unclassified activity stays inside input and the missing counter is labeled instead of shown as zero.</p>
      {filter_controls}
      {unknown_model_note}
      {preserved_history_note}
      {replay_quarantine_note}
      {totals_line}
      {chart_html}
      <div class="subsection-title">By platform</div>
      <div class="table-wrap"><table><thead><tr><th>Platform</th><th>Total tokens</th><th>Input after reported cache</th><th>Output</th><th>Cache writes</th><th>Cache reads</th><th>Est. cost</th><th>Sessions</th><th>Models</th></tr></thead><tbody>{by_platform_rows}</tbody></table></div>
      <div class="subsection-title with-controls"><span>By model</span><span class="sort-controls">Sort: {model_sort_control}</span></div>
      {by_model_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Agent</th><th>Provider / model</th><th>Usage records</th><th>Total tokens</th><th>Input after reported cache</th><th>Output</th><th>Cache writes</th><th>Cache reads</th><th>Est. cost</th></tr></thead><tbody>{by_model_rows}</tbody></table></div>
      <div class="subsection-title">By period</div>
      {by_period_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Period</th><th>Total tokens</th><th>Input after reported cache</th><th>Output</th><th>Cache writes</th><th>Cache reads</th><th>Est. cost</th><th>Sessions</th><th>Usage rows</th></tr></thead><tbody>{by_period_rows}</tbody></table></div>
    </section>

    <section class="section" id="usage-basics">
      <div class="section-header"><h2>Usage basics</h2></div>
      <p class="section-note">Store-wide confidence summaries over ALL saved usage rows — the filters above do not apply here. Token counts in these two tables include cache reads and are labeled as such.</p>
      <div class="split-grid">
        <div class="inline-panel"><div class="inline-title">Usage confidence</div><div class="table-wrap"><table><thead><tr><th>Confidence</th><th>Rows</th><th>Tokens incl. caches</th></tr></thead><tbody>{usage_confidence_table_rows}</tbody></table></div></div>
        <div class="inline-panel"><div class="inline-title">Cost confidence</div><div class="table-wrap"><table><thead><tr><th>Confidence</th><th>Rows</th><th>Tokens incl. caches</th><th>Cost</th></tr></thead><tbody>{cost_confidence_table_rows}</tbody></table></div></div>
      </div>
    </section>
"""


# ---------------------------------------------------------------------------
# Phase 3.5c — the Sessions explorer filters (PRD §6.2). Same rules as the
# /tokens filters: whitelisted choices (unknown values fall back to their
# defaults, never a 500), project validated against labels actually present
# (an unknown label renders the EMPTY result with the filter echoed — never a
# guess), every combination a URL, and the 40-row cap applies AFTER the
# filters with the honest "Showing N of M (filtered from T)" restatement.
# ---------------------------------------------------------------------------

SESSION_JOIN_FILTER_CHOICES = ("attributed", "context", "ambiguous", "unjoined")
SESSION_KIND_FILTER_CHOICES = ("grouped", "roots", "all-flat")
# /sessions display order: newest-first browse (default) vs the attribution-first
# triage order shared with the overview data. Kept separate from
# _session_display_sort_key so changing the browse order never moves other views.
SESSION_SORT_CHOICES = ("recent", "attributed")
# Optional lens: only sessions with recorded agentacct work (sections/checks).
SESSION_WORK_FILTER_CHOICES = ("all", "recorded")
# The browse can page past the default slice; "Show more" raises the cap.
SESSION_BROWSE_SHOW_CHOICES = (40, 100, 300, 1000)


def _session_join_filter_bucket(entry: dict[str, Any]) -> str:
    """Which join-filter pill a rollup entry belongs to — derived from the
    ledger's OWN state vocabulary plus its client-log-evidence block, exactly
    the facts the row's chip renders (never re-matched, no new states):

    - "attributed"/"ambiguous": those ledger states verbatim;
    - "context": every state whose chip claims recorded-but-unallocated
      context — ``context_only`` (context matched; not allocated),
      ``sections_only`` (sections recorded; no usage imported), and the
      unjoined-with-client-log-evidence override (MCP context recorded in
      this session; not allocated);
    - "unjoined": the bare no-context chip family (No MCP context /
      Pre-instrumentation / other_project / project-level-only).

    Every entry lands in exactly ONE bucket, so the four pills partition
    "all" — filtered counts can always be reconciled by hand.
    """

    join = entry.get("join") if isinstance(entry.get("join"), dict) else {}
    state = str(join.get("state") or "")
    if state in {"attributed", "ambiguous"}:
        return state
    if state in {"context_only", "sections_only"}:
        return "context"
    evidence = join.get("client_log_evidence") if isinstance(join.get("client_log_evidence"), dict) else {}
    if state == "unjoined" and int(evidence.get("evidenced_event_count") or 0) > 0:
        return "context"
    return "unjoined"


def _session_projects_present(rollup_sessions: list[dict[str, Any]]) -> list[str]:
    """Sorted distinct project labels present in the rollup — the whitelist
    the project filter pills come from. Labels are the ledger's own
    pre-redacted last-path-segment labels; nothing here touches a path."""

    return sorted(
        {str(project) for entry in rollup_sessions if isinstance(entry, dict) and (project := entry.get("project"))}
    )


def _session_last_activity_in_range(entry: dict[str, Any], *, range_start: date, today: date) -> bool:
    """days=N keeps entries whose last-activity LOCAL day falls in the
    trailing N days (same rule as usage_cube.filter_usage_records). Entries
    whose last activity fails the bad-timestamp guard (absent or absurd) are
    excluded from bounded ranges — a bounded range cannot honestly claim a
    session with no usable date; days=all keeps them."""

    day = usage_bucket_date(entry.get("last_activity_at"))
    return day is not None and range_start <= day <= today


def _work_item_matches_filters(
    item: dict[str, Any], *, client: str, project: str, range_start: date | None, today: date | None
) -> bool:
    """Work-items filter rule (PRD §6.2 "filter-aware"): the client, project,
    and days predicates apply through the item's OWN fields (item client,
    item project label, item update time). Standard filter semantics: an
    item missing a filtered key is excluded by that specific filter but
    always present under "all". The join filter never applies here — join
    state is a session-state concept, not a work-item one."""

    if client != "all" and str(item.get("client") or "") != client:
        return False
    if project != "all" and str(item.get("project_dir") or "") != project:
        return False
    if range_start is not None:
        day = usage_bucket_date(item.get("updated_at"))
        if day is None or not (range_start <= day <= (today or date.today())):
            return False
    return True


def _sessions_filter_controls_html(
    *,
    client: str,
    project: str,
    url_project: str,
    join: str,
    kind: str,
    days: str,
    sort: str,
    work: str,
    pill_projects: list[str],
    project_pill_note: str,
    esc: Any,
) -> str:
    """Filter pill rows for /sessions — the same _sort_control link pills as
    /tokens, so every filter combination is a shareable URL. Project pills
    are capped with an honest overflow note, and ``url_project`` (never an
    unknown/unvalidated value) is what gets re-encoded into pill URLs."""

    params = {
        "client": client,
        "project": url_project,
        "join": join,
        "kind": kind,
        "days": days,
    }
    # Only non-default sort/work ride the other pills' URLs, so default-page
    # links stay clean and shareable (and each pill preserves active state).
    if sort != "recent":
        params["sort"] = sort
    if work != "all":
        params["work"] = work
    client_control = _sort_control(
        current=client,
        options=[("all", "All platforms"), *[(name, _human_client(name)) for name in KNOWN_USAGE_CLIENTS]],
        param="client",
        extra=params,
        esc=esc,
        base="/sessions",
    )
    project_control = _sort_control(
        current=project,
        options=[("all", "All projects"), *[(name, name) for name in pill_projects]],
        param="project",
        extra=params,
        esc=esc,
        base="/sessions",
    ) + project_pill_note
    join_control = _sort_control(
        current=join,
        options=[
            ("all", "All join states"),
            ("attributed", "Attributed"),
            ("context", "Context recorded; not allocated"),
            ("ambiguous", "Ambiguous"),
            ("unjoined", "No MCP context"),
        ],
        param="join",
        extra=params,
        esc=esc,
        base="/sessions",
    )
    kind_control = _sort_control(
        current=kind,
        options=[
            ("grouped", "Grouped (children nested)"),
            ("roots", "Roots only"),
            ("all-flat", "All sessions, flat"),
        ],
        param="kind",
        extra=params,
        esc=esc,
        base="/sessions",
    )
    days_control = _sort_control(
        current=days,
        options=[("7", "7 days"), ("30", "30 days"), ("90", "90 days"), ("all", "All time")],
        param="days",
        extra=params,
        esc=esc,
        base="/sessions",
    )
    sort_control = _sort_control(
        current=sort,
        options=[("recent", "Newest first"), ("attributed", "Attributed first")],
        param="sort",
        extra=params,
        esc=esc,
        base="/sessions",
    )
    work_control = _sort_control(
        current=work,
        options=[("all", "All sessions"), ("recorded", "Recorded work only")],
        param="work",
        extra=params,
        esc=esc,
        base="/sessions",
    )
    return (
        '<div class="filter-rows">'
        f'<div class="filter-row"><span class="filter-label">Order</span>{sort_control}</div>'
        f'<div class="filter-row"><span class="filter-label">Platform</span>{client_control}</div>'
        f'<div class="filter-row"><span class="filter-label">Project</span>{project_control}</div>'
        f'<div class="filter-row"><span class="filter-label">Join state</span>{join_control}</div>'
        f'<div class="filter-row"><span class="filter-label">Sessions</span>{kind_control}</div>'
        f'<div class="filter-row"><span class="filter-label">Work</span>{work_control}</div>'
        f'<div class="filter-row"><span class="filter-label">Last activity</span>{days_control}</div>'
        "</div>"
    )


def _active_session_filter_labels(
    client: str, project: str, join: str, days: str, work: str = "all"
) -> list[str]:
    """Human phrases for the non-default filters — named in the empty state
    and the filtered-count note so a reader always knows what excluded rows."""

    labels: list[str] = []
    if client != "all":
        labels.append(f"platform {_human_client(client)}")
    if project != "all":
        labels.append(f"project {project}")
    if join != "all":
        join_names = {
            "attributed": "join state attributed",
            "context": "join state context recorded; not allocated",
            "ambiguous": "join state ambiguous",
            "unjoined": "join state no MCP context",
        }
        labels.append(join_names.get(join, f"join state {join}"))
    if work == "recorded":
        labels.append("recorded work only")
    if days != "all":
        labels.append(f"last activity in the last {days} days")
    return labels


def _sessions_body(
    data: _DashboardPageData,
    *,
    client: str = "all",
    project: str = "all",
    join: str = "all",
    kind: str = "grouped",
    days: str = "30",
    sort: str = "recent",
    work: str = "all",
    show: int = SESSION_ROLLUP_DISPLAY_LIMIT,
) -> str:
    """Sessions `/sessions` (PRD §6.2): the filterable session explorer +
    work items + attention + reconciliation + ledger insights.

    Filters apply to the SESSION LIST and the WORK ITEMS section (each
    through its own fields — PRD §6.2 "filter-aware"; the join filter is a
    session-state concept and never applies to work items). The KPI cards
    and the attention/reconciliation/insights sections stay store-wide
    (their notes say so). ``kind`` picks the row population: grouped
    (default — top-level rows with children nested as labeled lines), roots
    (root-kind sessions only), all-flat (every rollup entry as its own row;
    lineage stays visible via child-of chips). ``sort`` orders the browse
    (recent = newest-first, the default; attributed = the shared triage
    order). ``work`` optionally keeps only sessions with recorded work. The
    ``show`` cap applies AFTER filtering (default 40; "Show more" raises it);
    filtered counts restate "Showing N of M (filtered from T)"."""

    esc = data.esc
    rollup_summary = data.rollup_summary
    session_short_labels = data.session_short_labels

    client = _safe_choice(client, {"all", *KNOWN_USAGE_CLIENTS}, "all")
    join = _safe_choice(join, {"all", *SESSION_JOIN_FILTER_CHOICES}, "all")
    kind = _safe_choice(kind, set(SESSION_KIND_FILTER_CHOICES), "grouped")
    days = _safe_choice(days, set(USAGE_CUBE_DAYS_CHOICES), "30")
    sort = _safe_choice(sort, set(SESSION_SORT_CHOICES), "recent")
    work = _safe_choice(work, set(SESSION_WORK_FILTER_CHOICES), "all")
    try:
        show = int(show)
    except (TypeError, ValueError):
        show = SESSION_ROLLUP_DISPLAY_LIMIT
    show = max(SESSION_ROLLUP_DISPLAY_LIMIT, min(show, SESSION_BROWSE_SHOW_CHOICES[-1]))
    project = str(project or "all")
    projects_present = _session_projects_present(data.rollup_sessions)
    project_known = project == "all" or project in projects_present
    # An unknown project value is named ONCE in the note (escaped) and never
    # re-encoded into pill URLs — same rule as the /tokens model filter.
    url_project = project if project_known else "all"

    if kind == "roots":
        # Root-kind sessions only: drops the top-level rows that exist for
        # orphaned children / deep lineage / internal (auto-review) sessions.
        population = [
            entry
            for entry in data.top_level_sessions
            if str(entry.get("session_kind") or "") not in {"child", "internal"}
        ]
        kind_noun = "root sessions"
    elif kind == "all-flat":
        # Every rollup entry as its own row — children stop nesting and keep
        # their child-of chips; nothing merges, nothing double-counts.
        population = sorted(
            (entry for entry in data.rollup_sessions if isinstance(entry, dict)),
            key=_session_display_sort_key,
        )
        kind_noun = "sessions (children as their own rows)"
    else:
        population = data.top_level_sessions
        kind_noun = "top-level sessions"

    days_filter = days_choice_to_int(days)
    today = date.today()
    range_start = today - timedelta(days=days_filter - 1) if days_filter is not None else None
    filtered_sessions = [
        entry
        for entry in population
        if (client == "all" or str(entry.get("client") or "") == client)
        and (project == "all" or str(entry.get("project") or "") == project)
        and (join == "all" or _session_join_filter_bucket(entry) == join)
        and (work == "all" or int(((entry.get("work") or {}).get("counts") or {}).get("total") or 0) > 0)
        and (range_start is None or _session_last_activity_in_range(entry, range_start=range_start, today=today))
    ]
    # Newest-first is the default browse order; sort=attributed restores the
    # shared attribution-first order (the population's pre-sorted order).
    if sort == "recent":
        filtered_sessions = sorted(
            filtered_sessions, key=lambda entry: -float(entry.get("last_activity_at") or 0.0)
        )

    # Project pills: ranked by how many rollup sessions carry the label,
    # capped with an honest overflow note (the whitelist the filter validates
    # against stays every label present in the rollup).
    project_session_counts: dict[str, int] = {}
    for entry in data.rollup_sessions:
        if isinstance(entry, dict) and (label := entry.get("project")):
            project_session_counts[str(label)] = project_session_counts.get(str(label), 0) + 1
    ranked_projects = sorted(projects_present, key=lambda name: (-project_session_counts.get(name, 0), name))
    pill_projects, project_pill_note = _capped_value_pills(ranked_projects, current=url_project, param="project")

    filter_controls = _sessions_filter_controls_html(
        client=client,
        project=project,
        url_project=url_project,
        join=join,
        kind=kind,
        days=days,
        sort=sort,
        work=work,
        pill_projects=pill_projects,
        project_pill_note=project_pill_note,
        esc=esc,
    )
    unknown_project_note = ""
    if not project_known:
        unknown_project_note = (
            f'<p class="section-note">Project <code>{esc(project)}</code> has no sessions in this store — showing '
            "the empty result for this filter (never a guess). Pick a project pill above.</p>"
        )
    active_filter_labels = _active_session_filter_labels(client, project, join, days, work)
    # The filter-aware empty state names what excluded the rows — but only
    # when filters actually excluded something. A store with no sessions at
    # all keeps the generic what-to-do-next empty state.
    filtered_empty_html = (
        '<div class="empty-state">No sessions match the current filters ('
        + esc(" · ".join(active_filter_labels))
        + "). Clear a filter pill above or extend the date range.</div>"
    ) if active_filter_labels and population else None

    session_rows_html, _default_cap_note, sessions_capped = _session_list_html(
        filtered_sessions, show, esc, empty_html=filtered_empty_html
    )
    # Filtered counts restate their basis (PRD §10.6): when the filters
    # excluded anything, the note names shown / matching / total — a plain
    # cap note alone would hide that a filter is active. The show cap applies
    # AFTER filtering, so "Showing N of M" always describes the filtered
    # population.
    if len(filtered_sessions) < len(population):
        session_cap_note = (
            f'<p class="section-note">Showing {esc(_fmt_int(sessions_capped["shown"]))} of '
            f'{esc(_fmt_int(sessions_capped["total"]))} matching session(s), filtered from '
            f"{esc(_fmt_int(len(population)))} {esc(kind_noun)}"
            f"{esc(' (' + '; '.join(active_filter_labels) + ')' if active_filter_labels else '')}.</p>"
        )
    else:
        session_cap_note = _cap_note(sessions_capped["shown"], sessions_capped["total"], kind_noun)
    # "Show more" raises the cap in place (preserves every active filter/sort).
    if sessions_capped["shown"] < sessions_capped["total"]:
        base_params = {
            "client": client, "project": url_project, "join": join, "kind": kind,
            "days": days, "sort": sort, "work": work,
        }
        more_pills = []
        for choice in SESSION_BROWSE_SHOW_CHOICES:
            if choice <= show or choice <= sessions_capped["shown"]:
                continue
            qs = "&".join(f"{k}={quote(str(v))}" for k, v in {**base_params, "show": choice}.items())
            label = "Show all" if choice >= sessions_capped["total"] else f"Show {choice}"
            more_pills.append(f'<a class="see-all" href="/sessions?{esc(qs)}">{esc(label)} →</a>')
            if choice >= sessions_capped["total"]:
                break
        if more_pills:
            session_cap_note += f'<p class="section-note">{" · ".join(more_pills)}</p>'

    rollup_total_sessions = int(rollup_summary.get("total_sessions") or 0)
    rollup_child_sessions = int(rollup_summary.get("child_sessions") or 0)
    rollup_sections_only = int(rollup_summary.get("sessions_with_sections_only") or 0)
    rollup_attributed_sessions = int(rollup_summary.get("attributed_sessions") or 0)
    attention_group_count = len(data.attention_groups)

    ledger_insights = data.ledger_insights
    usage_insight = ledger_insights.get("usage_attribution_summary") if isinstance(ledger_insights.get("usage_attribution_summary"), dict) else {}
    trust_insight = ledger_insights.get("trust_summary") if isinstance(ledger_insights.get("trust_summary"), dict) else {}
    insight_blind_spots = ledger_insights.get("blind_spots") if isinstance(ledger_insights.get("blind_spots"), list) else []
    insight_next_actions = ledger_insights.get("top_next_actions") if isinstance(ledger_insights.get("top_next_actions"), list) else []
    insight_health = str(ledger_insights.get("ledger_health") or "partial")
    insight_evidence_rate = _fmt_percent(
        int(trust_insight.get("evidence_backed_completed_count") or 0),
        int(trust_insight.get("completed_work_count") or 0),
    )

    def insight_blind_spot_detail(blind_spot: dict[str, Any]) -> str:
        tokens = blind_spot.get("tokens")
        cost = blind_spot.get("estimated_cost_usd")
        if tokens is None and cost is None:
            return "Usage unknown / not attributed"
        # Fresh figure leads (locked fresh-headline rule); the
        # everything-included figure is always labeled, never a bare
        # "tokens" number that resurrects the cache-inflated headline.
        return (
            f"{_fmt_int(blind_spot.get('fresh_tokens'))} fresh tokens; "
            f"{_fmt_int(tokens)} incl. caches; "
            f"{_fmt_optional_usd_text(cost)} estimated, not provider bill"
        )

    insight_blind_spot_rows = "\n".join(
        "<li>"
        f"<strong>{esc(_display_count_label(blind_spot.get('type')))}</strong>"
        f" <span class=\"status {_evidence_status_class('failed' if blind_spot.get('severity') == 'high' else 'weak')}\">{esc(_display_count_label(blind_spot.get('severity')))}</span>"
        f"<br><span>{esc(blind_spot.get('summary') or '')}</span>"
        f"<br><span class=\"note\">{esc(insight_blind_spot_detail(blind_spot))}</span>"
        "</li>"
        for blind_spot in insight_blind_spots[:3]
    ) or "<li>No major blind spots.</li>"
    insight_next_action_rows = "\n".join(
        f"<li>{esc(action)}</li>"
        for action in insight_next_actions[:3]
    ) or "<li>No urgent reconciliation action.</li>"

    # 2h: work items from ledger work_items — filter-aware (PRD §6.2): the
    # client/project/days filters apply through each item's OWN fields; the
    # join filter never applies (session-state concept). Attributed items
    # first, then newest; the cap applies AFTER filtering.
    work_items_ordered = sorted(
        (
            item
            for item in data.work_items
            if _work_item_matches_filters(item, client=client, project=project, range_start=range_start, today=today)
        ),
        key=lambda item: (
            0 if int(item.get("linked_usage_records") or 0) > 0 else 1,
            -float(item.get("updated_at") or 0.0),
        ),
    )
    work_items_capped = capped_rows(work_items_ordered, WORK_ITEM_DISPLAY_LIMIT)
    if len(work_items_ordered) < len(data.work_items):
        work_item_cap_note = (
            f'<p class="section-note">Showing {esc(_fmt_int(work_items_capped["shown"]))} of '
            f'{esc(_fmt_int(work_items_capped["total"]))} matching work item(s), filtered from '
            f"{esc(_fmt_int(len(data.work_items)))} work items.</p>"
        )
    else:
        work_item_cap_note = _cap_note(work_items_capped["shown"], work_items_capped["total"], "work items")
    if data.work_items and not work_items_ordered:
        work_items_empty = (
            '<tr><td colspan="6">No work items match the current filters. '
            "Clear a filter pill above or extend the date range.</td></tr>"
        )
    else:
        work_items_empty = (
            '<tr><td colspan="6">No MCP section work items yet. Record sections during agent work to see work items here.</td></tr>'
        )
    work_item_rows = "\n".join(
        _work_item_row_html(item, esc, session_short_labels) for item in work_items_capped["rows"]
    ) or work_items_empty

    # 2i: grouped attention (one row per cause; headline number = groups).
    attention_group_rows = "\n".join(
        _attention_group_row_html(group, esc) for group in data.attention_groups if isinstance(group, dict)
    ) or (
        '<tr><td colspan="4">No reconciliation attention items. Imported usage, MCP work, and evidence are either linked or absent.</td></tr>'
    )

    work_by_id = {str(item.get("work_id") or ""): item for item in data.work_items if item.get("work_id")}

    def reconciliation_usage_cell(row: dict[str, Any]) -> str:
        session_label = _session_display_label(row.get("client"), row.get("client_session_id"), session_short_labels)
        session_html = f"<code>{esc(session_label)}</code>" if session_label else '<span class="note">no session id</span>'
        return (
            f"<strong>Usage truth</strong><br>{esc(_human_client(row.get('client')))} / "
            f"{esc(_display_provider_model(row.get('provider'), row.get('model')))}<br>"
            f"{session_html}<br>"
            f"<span class=\"note\">{esc(_fmt_int(row.get('fresh_tokens')))} fresh tokens; "
            f"{esc(_fmt_int(row.get('total_tokens')))} total incl. {esc(_fmt_int(row.get('cache_read_tokens')))} cache reads; "
            f"{esc(_fmt_optional_usd_text(row.get('estimated_cost_usd')))}; "
            f"{esc(_display_count_label(row.get('usage_confidence')))} usage / "
            f"{esc(_display_count_label(row.get('cost_confidence')))} cost</span>"
        )

    def reconciliation_work_cell(item: dict[str, Any] | None, *, fallback: str) -> str:
        if item is None:
            return f"<strong>MCP work</strong><br>{esc(fallback)}"
        return (
            f"<strong>{esc(item.get('title'))}</strong><br>"
            f"<span class=\"status {_work_status_class(item.get('latest_status'))}\">{esc(item.get('latest_status'))}</span>"
            f"<span class=\"note\"> {esc(_fmt_time(item.get('updated_at')))}</span>"
        )

    def reconciliation_evidence_cell(item: dict[str, Any] | None) -> str:
        if item is None:
            return "No work evidence linked"
        return (
            f"<span class=\"status {_evidence_status_class(item.get('evidence_status'))}\">{esc(item.get('evidence_status'))}</span><br>"
            f"<span class=\"note\">{esc(_evidence_note_for_item(item))}</span>"
        )

    # 2j: rows consume the ledger's attributed-first ordering verbatim; the
    # cap goes through capped_rows so truncation is always announced (the
    # silent [:120] cap is how the one attributed row used to disappear).
    # Work items without usage live in #work-items, not as prelude rows.
    reconciliation_capped = capped_rows(data.usage_reconciliation, RECONCILIATION_DISPLAY_LIMIT)
    reconciliation_cap_note = _cap_note(reconciliation_capped["shown"], reconciliation_capped["total"], "usage rows")
    reconciliation_rows: list[str] = []
    for row in reconciliation_capped["rows"]:
        state = row.get("usage_reconciliation_state") or row.get("usage_join_state")
        work = work_by_id.get(str(row.get("work_id") or ""))
        if work is not None:
            work_fallback = "Attached work item"
        elif state == "ambiguous":
            work_fallback = "Multiple MCP sections share this context"
        elif state == "context_matched_unallocated":
            work_fallback = "MCP context matched; not allocated"
        else:
            work_fallback = "No MCP work context matched"
        reconciliation_rows.append(
            "<tr>"
            f"<td>{reconciliation_usage_cell(row)}</td>"
            f"<td>{reconciliation_work_cell(work, fallback=work_fallback)}</td>"
            f"<td>{reconciliation_evidence_cell(work)}</td>"
            f"<td><span class=\"status {_join_state_class(state)}\">{esc(_join_state_label(state))}</span><br>"
            f"<span class=\"note\">{esc(row.get('join_reason') or '')}</span></td>"
            f"<td>{esc(row.get('recommended_next_step') or '')}</td>"
            "</tr>"
        )
    product_reconciliation_rows = "\n".join(reconciliation_rows) or (
        '<tr><td colspan="5">No imported usage recorded yet. Run local usage import and record MCP sections to begin reconciliation.</td></tr>'
    )

    has_imported_usage_rows = bool(data.usage_view.saved_records)
    if has_imported_usage_rows:
        usage_metric_cards = f"""
        <div class="metric bridge"><div class="label">Attributed usage</div><div class="value">{esc(_fmt_int(usage_insight.get('attributed_fresh_tokens')))}</div><div class="note">fresh tokens; {esc(_fmt_int(usage_insight.get('attributed_tokens')))} incl. caches; {esc(_fmt_int(usage_insight.get('attributed_count')))} usage rows; {esc(_fmt_optional_usd_text(usage_insight.get('attributed_cost_usd')))} estimated, not provider bill</div></div>
        <div class="metric warn"><div class="label">Unknown / unattributed usage</div><div class="value">{esc(_fmt_int(usage_insight.get('unknown_or_unattributed_fresh_tokens')))}</div><div class="note">fresh tokens; {esc(_fmt_int(usage_insight.get('unknown_or_unattributed_tokens')))} incl. caches; {esc(_fmt_int(int(usage_insight.get('ambiguous_count') or 0) + int(usage_insight.get('context_matched_unallocated_count') or 0) + int(usage_insight.get('usage_without_mcp_context_count') or 0)))} usage rows; {esc(_fmt_optional_usd_text(usage_insight.get('unknown_or_unattributed_cost_usd')))} estimated, not provider bill</div></div>"""
        attributed_sessions_value = (
            f"{_fmt_int(rollup_attributed_sessions)} of {_fmt_int(rollup_total_sessions)}"
        )
        attributed_sessions_note = (
            f"{_fmt_int(usage_insight.get('attributed_fresh_tokens'))} fresh tokens attributed "
            f"({_fmt_int(usage_insight.get('attributed_tokens'))} incl. caches)"
        )
    else:
        usage_metric_cards = """
        <div class="metric"><div class="label">Attributed usage</div><div class="value">Unavailable</div><div class="note">No imported usage rows; token and cost totals are unknown, not zero.</div></div>
        <div class="metric"><div class="label">Unknown / unattributed usage</div><div class="value">Unavailable</div><div class="note">No imported usage rows; nothing has been measured for allocation.</div></div>"""
        attributed_sessions_value = "Usage unavailable"
        attributed_sessions_note = (
            "No imported usage rows; session presence does not imply zero tokens or zero cost."
        )

    # An empty ledger (no usage rows, no sessions, no work items) has nothing
    # to grade: the health chip + zero-value cards would read "good · 0 ·
    # $0.00" on a store where nothing was measured (PRD §10.6 — empty states
    # say why and what to do next, never a measured-looking zero).
    if not data.usage_view.saved_records and not data.rollup_sessions and not data.work_items:
        ledger_insights_section = (
            '<section class="section" id="ledger-insights">\n'
            '      <div class="section-header"><h2>Ledger insights</h2></div>\n'
            '      <div class="empty-state">Nothing to reconcile yet. Once usage rows and MCP work sections exist, '
            "this section grades how usage, work, and machine-check evidence line up (attributed vs unattributed "
            "tokens, evidence-backed completion, blind spots). Import usage (Refresh &amp; save usage) and record "
            "MCP sections during agent work to populate it.</div>\n"
            "    </section>"
        )
    else:
        ledger_insights_section = f"""<section class="section" id="ledger-insights">
      <div class="section-header"><h2>Ledger insights</h2><span class="status {_ledger_health_status_class(insight_health)}">{esc(_display_count_label(insight_health))}</span></div>
      <p class="section-note">{esc(ledger_insights.get('ledger_health_reason') or '')}</p>
      <div class="metric-grid">
        {usage_metric_cards}
        <div class="metric bridge"><div class="label">Evidence-backed completed</div><div class="value">{esc(insight_evidence_rate)}</div><div class="note">{esc(_fmt_int(trust_insight.get('evidence_backed_completed_count')))} / {esc(_fmt_int(trust_insight.get('completed_work_count')))} completed work items</div></div>
      </div>
      <div class="split-grid">
        <div class="inline-panel"><div class="inline-title">Top blind spots</div><ul>{insight_blind_spot_rows}</ul></div>
        <div class="inline-panel"><div class="inline-title">Top next actions</div><ul>{insight_next_action_rows}</ul></div>
      </div>
    </section>"""

    return f"""    <nav class="nav" aria-label="Sessions page sections">
      <a href="#sessions">Sessions</a>
      <a href="#work-items">Work items</a>
      <a href="#attention">Needs attention</a>
      <a href="#reconciliation">Reconciliation</a>
      <a href="#ledger-insights">Ledger insights</a>
    </nav>

    <section class="section" id="sessions">
      <div class="section-header"><h2>Sessions</h2></div>
      <p class="section-note">One row per client session, grouped by exact session key only — nothing here allocates usage to work. Direct subagent child sessions are listed under their parent row and never merged into it; deeper lineage renders as its own row with a child-of chip. Fresh tokens = input + output; cache writes and cache reads stay separate.</p>
      <div class="metric-grid">
        <div class="metric log"><div class="label">Sessions</div><div class="value">{esc(_fmt_int(len(data.top_level_sessions)))}</div><div class="note">{esc(_fmt_int(rollup_total_sessions))} total · {esc(_fmt_int(rollup_child_sessions))} subagent children · {esc(_fmt_int(rollup_sections_only))} sections-only</div></div>
        <div class="metric bridge"><div class="label">Attributed sessions</div><div class="value">{esc(attributed_sessions_value)}</div><div class="note">{esc(attributed_sessions_note)}</div></div>
        <div class="metric warn"><div class="label">Needs attention</div><div class="value"><a href="#attention">{esc(_fmt_int(attention_group_count))}</a></div><div class="note">{esc(_fmt_int(data.attention_total_items))} item(s) grouped by cause</div></div>
      </div>
      <p class="section-note">Filters apply to the session list and the Work items section below; the cards above and the attention / reconciliation / insights sections stay store-wide.</p>
      {filter_controls}
      {unknown_project_note}
      {session_cap_note}
      <div class="session-list">{session_rows_html}</div>
    </section>

    <section class="section" id="work-items">
      <div class="section-header"><h2>Work items</h2></div>
      <p class="section-note">Agent-reported MCP sections with status, machine-check evidence, and attributed usage. Attributed items sort first. A dash means agentacct refuses to guess: usage attaches only through the canonical join. The platform, project, and date filters above apply here through each item's own fields (an item missing a filtered field is excluded by that filter); the join filter is a session-state concept and does not apply to work items.</p>
      {work_item_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Work item</th><th>Status</th><th>Evidence</th><th>Attributed usage</th><th>Join state</th><th>Details</th></tr></thead><tbody>{work_item_rows}</tbody></table></div>
    </section>

    <section class="section" id="attention">
      <div class="section-header"><h2>Needs attention</h2><span class="note">{esc(_fmt_int(attention_group_count))} cause group(s) · {esc(_fmt_int(data.attention_total_items))} item(s)</span></div>
      <p class="section-note">Grouped by cause: the headline counts causes, not flooded rows. Example references are bounded and redacted.</p>
      <div class="table-wrap"><table><thead><tr><th>Severity</th><th>Cause</th><th>Items</th><th>Recommended next step</th></tr></thead><tbody>{attention_group_rows}</tbody></table></div>
    </section>

    <section class="section" id="reconciliation">
      <div class="section-header"><h2>Reconciliation</h2></div>
      <p class="section-note">One row answers how usage truth, MCP work, evidence, attribution, and next action line up. Attributed rows sort first. Context matched and ambiguous rows are not split into section-level billing.</p>
      {reconciliation_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Usage truth</th><th>MCP work</th><th>Evidence</th><th>Attribution</th><th>Action</th></tr></thead><tbody>{product_reconciliation_rows}</tbody></table></div>
    </section>

    {ledger_insights_section}
"""


def _raw_body(
    data: _DashboardPageData,
    *,
    runs: list[dict[str, Any]],
    usage_sources: list[UsageSourceDiscovery],
    tools_sort: str,
    cost_sort: str,
    activity_sort: str,
    timeline_view: str,
) -> str:
    """Raw `/raw`: the debug surface, moved verbatim (locked decision: the
    raw tab is untouched this phase). This is the ONLY page whose renderer
    may consume the live scan (`usage_view.local_records` / sources)."""

    esc = data.esc
    events = data.events
    session_short_labels = data.session_short_labels
    usage_view = data.usage_view
    local_by_client = usage_view.local_by_client

    tools_sort = _safe_choice(tools_sort, {"agent", "sessions", "tokens", "cost"}, "tokens")
    cost_sort = _safe_choice(cost_sort, {"agent", "input", "output", "cache_create", "cache_read", "total"}, "total")
    activity_sort = _safe_choice(activity_sort, {"newest", "oldest", "cost", "source"}, "newest")
    timeline_view = _safe_choice(timeline_view, TIMELINE_VIEWS, "grouped")
    sort_params = {
        "tools_sort": tools_sort,
        "cost_sort": cost_sort,
        "activity_sort": activity_sort,
        "timeline_view": timeline_view,
    }
    tools_sort_control = _sort_control(
        current=tools_sort,
        options=[("tokens", "Tokens"), ("cost", "Cost"), ("sessions", "Sessions"), ("agent", "Agent")],
        param="tools_sort",
        extra=sort_params,
        esc=esc,
    )
    activity_sort_control = _sort_control(
        current=activity_sort,
        options=[("newest", "Newest"), ("oldest", "Oldest"), ("cost", "Cost"), ("source", "Agent")],
        param="activity_sort",
        extra=sort_params,
        esc=esc,
    )
    timeline_view_control = _sort_control(
        current=timeline_view,
        options=[("grouped", "Grouped"), ("all", "Everything"), ("mcp", "MCP only"), ("usage", "Usage only")],
        param="timeline_view",
        extra=sort_params,
        esc=esc,
    )

    def activity_sort_key(record: DashboardUsageRecord) -> Any:
        if activity_sort == "source":
            return (record.client, record.model or "")
        if activity_sort == "cost":
            return float(record.estimated_cost_usd or 0.0)
        return _usage_record_time(record)

    raw_activity_events = [event for event in events if not _is_usage_activity(event)]
    usage_activity_records = sorted(
        [*usage_view.saved_records, *usage_view.excluded_saved_records],
        key=activity_sort_key,
        reverse=activity_sort not in {"oldest", "source"},
    )
    raw_activity_events = sorted(raw_activity_events, key=lambda event: _session_activity_time(event), reverse=True)
    raw_events_capped = capped_rows(raw_activity_events, 50)
    raw_event_cap_note = _cap_note(raw_events_capped["shown"], raw_events_capped["total"], "events")
    activity_capped = capped_rows(usage_activity_records, 100)
    activity_cap_note = _cap_note(activity_capped["shown"], activity_capped["total"], "usage records")
    cost_events_capped = capped_rows(data.cost_events, 50)
    cost_event_cap_note = _cap_note(cost_events_capped["shown"], cost_events_capped["total"], "budget decisions")

    command_rows = "\n".join(
        "<tr>"
        f"<td>{esc(_fmt_time(run.get('started_at') or run.get('created_at')))}</td>"
        f"<td>{esc(run.get('status'))}</td>"
        f"<td>{esc(_fmt_duration(run.get('duration_seconds')))}</td>"
        f"<td><code>{esc(' '.join(str(part) for part in (run.get('command') or [])))}</code></td>"
        f"<td><code>{esc(run.get('run_id'))}</code></td>"
        "</tr>"
        for run in runs
    ) or '<tr><td colspan="5">No commands launched through agentacct yet.</td></tr>'

    def activity_usage_cell(record: DashboardUsageRecord) -> str:
        if not record.usage_additive:
            raw_input = int(record.raw_cumulative_input_tokens or 0)
            raw_output = int(record.raw_cumulative_output_tokens or 0)
            raw_cached = int(record.raw_cumulative_cached_input_tokens or 0)
            raw_total = raw_input + raw_output + raw_cached
            return (
                f'<strong>Held from totals</strong><br><span class="note">{esc(_fmt_int(raw_total))} raw cumulative '
                f'({esc(_fmt_int(raw_input))} input / {esc(_fmt_int(raw_output))} output / '
                f'{esc(_fmt_int(raw_cached))} cached); parent replay baseline not yet proven.</span>'
            )
        return (
            f"{esc(_fmt_int(record.total_tokens_including_cached))} total"
            f"<br><span class=\"note\">{esc(_fmt_int(record.input_tokens))} in / {esc(_fmt_int(record.output_tokens))} out / "
            f"{esc(_fmt_int(record.cache_creation_input_tokens))} cache create / {esc(_fmt_int(record.cache_read_input_tokens))} cache read / {esc(_record_reasoning_label(record))}</span>"
        )

    activity_rows = "\n".join(
        "<tr>"
        f"<td>{esc(_fmt_time(_usage_record_time(record)))}</td>"
        f"<td>{esc(_human_client(record.client))}<br><span class=\"note\">{esc(_record_kind_label(record.session_kind))}</span></td>"
        f"<td>{_record_session_label_cell(record, esc, session_short_labels)}</td>"
        f"<td>{esc(_display_provider_model(record.provider, record.model))}</td>"
        f"<td>{activity_usage_cell(record)}</td>"
        f"<td>{_fmt_optional_usd(record.estimated_cost_usd)}</td>"
        f"<td>{esc(_display_count_label(record.usage_confidence))} / {esc(_display_count_label(record.cost_confidence))}</td>"
        "</tr>"
        for record in activity_capped["rows"]
    ) or '<tr><td colspan="7">No saved AI sessions yet. Use Refresh & save usage to import local usage summaries.</td></tr>'

    raw_event_rows = "\n".join(
        "<tr>"
        f"<td>{esc(_fmt_time(event.get('created_at')))}</td>"
        f"<td>{esc(event.get('source'))}</td>"
        f"<td>{_raw_event_activity_cell(event, esc)}</td>"
        f"<td>{esc(_display_provider_model(event.get('provider'), event.get('model')))}</td>"
        f"<td>{esc(_fmt_int(event.get('estimated_input_tokens')))} input / {esc(_fmt_int(event.get('estimated_output_tokens')))} output</td>"
        f"<td>{_fmt_optional_usd(event.get('estimated_cost_usd'))}</td>"
        f"<td>{esc(_display_count_label(event.get('usage_confidence')))} / {esc(_display_count_label(event.get('cost_confidence')))}</td>"
        f"<td><code>{esc(event.get('run_id'))}</code></td>"
        "</tr>"
        for event in raw_events_capped["rows"]
    ) or '<tr><td colspan="8">No non-usage agentacct events yet.</td></tr>'

    cost_event_rows = "\n".join(
        "<tr>"
        f"<td>{esc(_fmt_time(event.get('created_at')))}</td>"
        f"<td><code>{esc(event.get('run_id'))}</code></td>"
        f"<td>{esc(_display_provider_model(event.get('provider'), event.get('model')))}</td>"
        f"<td>{esc(event.get('decision'))}</td>"
        f"<td>{_fmt_optional_usd(event.get('estimated_cost_usd'))}</td>"
        f"<td>{esc(_display_count_label(event.get('usage_confidence')))} / {esc(_display_count_label(event.get('cost_confidence')))}</td>"
        "</tr>"
        for event in cost_events_capped["rows"]
    ) or '<tr><td colspan="6">No proxied API budget decisions yet.</td></tr>'

    found_usage_sources = [source for source in usage_sources if source.status == "found"]
    cost_breakdown = sorted(
        _cost_breakdown_rows(usage_view.local_records), key=_cost_breakdown_sort_key(cost_sort), reverse=cost_sort != "agent"
    )
    provider_usage = _provider_usage_rows(usage_view.saved_records)
    context_bridge = build_usage_context_bridge(events)
    bridge_links = context_bridge.get("links") if isinstance(context_bridge, dict) else []
    bridge_links = bridge_links if isinstance(bridge_links, list) else []
    unlinked_contexts = context_bridge.get("unlinked_contexts") if isinstance(context_bridge, dict) else []
    unlinked_contexts = unlinked_contexts if isinstance(unlinked_contexts, list) else []
    unlinked_contexts_capped = capped_rows(unlinked_contexts, 25)
    unlinked_context_cap_note = _cap_note(
        unlinked_contexts_capped["shown"], unlinked_contexts_capped["total"], "unlinked context events"
    )

    saved_by_client = usage_view.saved_by_client
    saved_observations_by_client: dict[str, int] = {}
    for _namespace, observation_client, _session_id in (
        selected_local_session_observation_source_identities(events)
    ):
        saved_observations_by_client[observation_client] = (
            saved_observations_by_client.get(observation_client, 0) + 1
        )

    def imported_count_for_client(client: str) -> int:
        if client == "cursor":
            return saved_observations_by_client.get(client, 0)
        return len(saved_by_client.get(client, []))

    def imported_count_cell(source: UsageSourceDiscovery) -> str:
        count = esc(_fmt_int(imported_count_for_client(source.client)))
        if source.client == "cursor":
            return f"{count} observation(s) saved"
        return f"{count} saved"

    def source_sort_key(source: UsageSourceDiscovery) -> Any:
        local_records = local_by_client.get(source.client, [])
        totals = _usage_record_totals(local_records)
        if tools_sort == "agent":
            return source.display_name.lower()
        if tools_sort == "sessions":
            return totals["records"] or source.session_count or 0
        if tools_sort == "cost":
            return totals["client_reported_cost_usd"] or totals["estimated_equivalent_cost_usd"]
        return totals["total_tokens_including_cached"]

    found_usage_sources = sorted(found_usage_sources, key=source_sort_key, reverse=tools_sort != "agent")

    def source_sessions_cell(source: UsageSourceDiscovery) -> str:
        records = local_by_client.get(source.client, [])
        if records:
            return _usage_record_count_cell(_usage_record_totals(records), esc)
        return esc(_fmt_int(source.session_count) if source.session_count is not None else "")

    provider_token_rows = "\n".join(
        "<tr>"
        f"<td>{esc(_display_count_label(provider))}</td>"
        f"<td>{_usage_record_count_cell(stats, esc)}</td>"
        f"<td>{esc(_fmt_int(stats.get('estimated_input_tokens', 0)))}</td>"
        f"<td>{esc(_fmt_int(stats.get('estimated_output_tokens', 0)))}</td>"
        f"<td>{esc(_fmt_int(stats.get('estimated_total_tokens', 0)))}</td>"
        f"<td>{esc(_fmt_int(stats.get('cache_creation_input_tokens', 0)))}</td>"
        f"<td>{esc(_fmt_int(stats.get('cache_read_input_tokens', 0)))}</td>"
        f"<td>{esc(_fmt_int(stats.get('reasoning_output_tokens', 0)))}</td>"
        f"<td>{esc(_fmt_int(stats.get('total_tokens_including_cached', 0)))}</td>"
        f"<td>{_fmt_usd(stats.get('estimated_cost_usd', 0))}</td>"
        "</tr>"
        for provider, stats in provider_usage
    ) or '<tr><td colspan="10">No provider token usage yet.</td></tr>'
    bridge_capped = capped_rows(bridge_links, 100)
    bridge_cap_note = _cap_note(bridge_capped["shown"], bridge_capped["total"], "usage records")
    bridge_status_notice = ""
    if context_bridge.get("health_status") == "degraded":
        bridge_status_notice = (
            '<div class="bridge-alert">'
            f"{esc(_fmt_int(context_bridge.get('context_matched_usage_records')))} of "
            f"{esc(_fmt_int(context_bridge.get('usage_records')))} usage rows match MCP context; "
            f"{esc(_fmt_int(context_bridge.get('attributed_usage_records')))} are attributed to work. "
            "Record exact client session or transcript ids before work sections to improve coverage."
            "</div>"
        )
    bridge_rows = "\n".join(
        "<tr>"
        f"<td><strong>{esc(_human_client(link.get('client')))}</strong><br>"
        f"<code>{esc(_short_session_label(link.get('client'), link.get('client_session_id'), session_short_labels))}</code><br>"
        f"<span class=\"note\">{esc(link.get('client_session_kind') or 'root')} session / {esc(_display_provider_model(link.get('provider'), link.get('model')))}</span></td>"
        f"<td>{esc(_fmt_int(link.get('total_tokens_including_cached')))} total<br>"
        f"<span class=\"note\">{esc(_fmt_optional_usd_text(link.get('estimated_cost_usd')))}; "
        f"{esc(_display_count_label(link.get('usage_confidence')))} usage / {esc(_display_count_label(link.get('cost_confidence')))} cost</span></td>"
        f"<td><span class=\"status {_bridge_join_status_class(link.get('join_confidence'))}\">{esc(_display_count_label(link.get('join_confidence')))}</span><br>"
        f"<span class=\"note\">{esc(_bridge_join_keys_label(link))}</span></td>"
        f"<td>{esc(_fmt_int(link.get('context_event_count')))} MCP events<br>"
        f"<span class=\"note\">{esc(_bridge_context_counts_label(link))}</span></td>"
        f"<td>{esc(_bridge_sections_label(link))}<br><span class=\"note\">{esc(_bridge_usage_debug_label(link))}</span></td>"
        "</tr>"
        for link in bridge_capped["rows"]
    ) or '<tr><td colspan="5">No imported usage records available for context bridging yet.</td></tr>'
    unlinked_context_rows = "\n".join(
        "<tr>"
        f"<td>{esc(_bridge_semantic_event_label(context))}</td>"
        f"<td>{esc(_human_client(context.get('client')))}<br>"
        f"<code>{esc(_short_session_label(context.get('client'), context.get('client_session_id'), session_short_labels))}</code></td>"
        f"<td>{esc(context.get('section_title') or context.get('section_id') or context.get('reporting_basis') or '')}</td>"
        f"<td>{esc(context.get('summary') or '')}</td>"
        "</tr>"
        for context in unlinked_contexts_capped["rows"]
    ) or '<tr><td colspan="4">No unlinked MCP context events.</td></tr>'
    cost_breakdown_rows = _cost_breakdown_table_rows(
        cost_breakdown, esc, "No list-price cost estimates available for the detected local models."
    )
    ready_source_rows = "\n".join(
        "<tr>"
        f"<td>{esc(source.display_name)}</td>"
        f"<td>{_source_status_badge(source, local_by_client.get(source.client, []))}</td>"
        f"<td>{source_sessions_cell(source)}</td>"
        f"<td>{_local_usage_cell(local_by_client.get(source.client, []), esc)}</td>"
        f"<td>{_local_cost_cell(local_by_client.get(source.client, []), esc)}</td>"
        f"<td>{_local_models_cell(local_by_client.get(source.client, []), esc)}</td>"
        f"<td>{_confidence_cell(source, local_by_client.get(source.client, []), esc)}</td>"
        f"<td>{imported_count_cell(source)}</td>"
        f"<td>{esc(_human_source_evidence(source.evidence))}</td>"
        "</tr>"
        for source in found_usage_sources
    ) or '<tr><td colspan="9">No local AI tool usage found yet.</td></tr>'
    missing_source_rows = "\n".join(
        "<tr>"
        f"<td>{esc(source.display_name)}</td>"
        f"<td>{_source_status_badge(source, local_by_client.get(source.client, []))}</td>"
        f"<td>{esc(_human_source_evidence(source.evidence))}</td>"
        f"<td><code>{esc('; '.join(source.paths))}</code>"
        f"{_source_notes_html(source, esc)}</td>"
        "</tr>"
        for source in usage_sources
        if source.status != "found"
    ) or '<tr><td colspan="4">All known local usage sources were detected.</td></tr>'

    # 2n: view-aware Work Timeline. Default 'grouped' collapses usage import
    # rows per (client, local day) so MCP sections stay visible; 'all' is the
    # raw row-level mix, one click away.
    timeline_view_entries = _timeline_entries_for_view(data.ledger["timeline"], timeline_view)
    timeline_capped = capped_rows(timeline_view_entries, TIMELINE_ROW_LIMIT)
    timeline_cap_note = _cap_note(timeline_capped["shown"], timeline_capped["total"], "timeline entries")
    timeline_rows = "\n".join(
        _timeline_row(entry, esc) for entry in timeline_capped["rows"]
    ) or '<tr><td colspan="7">No usage or MCP timeline events yet.</td></tr>'

    usage_over_time_table_rows, usage_over_time_cap_note = _usage_over_time_table_parts(usage_view.saved_records, esc)
    cost_confidence_table_rows = _cost_confidence_table_html(usage_view.saved_records, esc)
    debug_endpoint_rows = "\n".join(
        f'<tr><td><code>{esc(path)}</code></td><td>{esc(label)}</td></tr>'
        for path, label in [
            ("/overview", "Derived local work overview JSON"),
            ("/timeline", "Derived usage, work, and evidence timeline JSON"),
            ("/work-items", "Derived MCP section work item JSON"),
            ("/sessions", "Derived session rollup JSON (one entry per client session)"),
            ("/attention", "Derived attention groups JSON (one group per cause)"),
            ("/events", "Raw local event JSON"),
            ("/events/summary", "Aggregated local event JSON"),
            ("/usage/sources", "Detected local usage source JSON (read-only discovery scan)"),
            ("/ingestion/health", "Durable per-source scan receipts and watcher heartbeat"),
            ("/capabilities/agents", "Static evidence-backed agent capability manifest"),
            ("/usage/preview", "Read-only local usage preview JSON"),
            ("/usage/summary", "Usage cube JSON: tokens by platform, model, and period (client/model/days/granularity filters)"),
            ("/runs", "agentacct-launched command JSON"),
        ]
    )
    # Refusals are derived from the same live scan this page already consumes,
    # so they cover every rejection still on disk — including ones recorded
    # long before this surface existed.
    refused_recording_html = _refused_recording_html(usage_view.refused_recording, esc)

    return f"""    <section class="section" id="debug">
      <div class="section-header"><h2>Raw Data / Debug</h2></div>
      <p class="section-note">Raw logs/import rows, MCP semantic events, diagnostics, command reports, and context-bridge internals.</p>
      <div class="subsection-title">Current run flow</div>
      <div class="workflow">
        <div class="step"><strong>1. Detect local tools</strong><code>agentacct usage discover-sources</code></div>
        <div class="step"><strong>2. Preview or refresh</strong><code>agentacct serve</code></div>
        <div class="step"><strong>3. Save usage to Activity log</strong><code>agentacct usage import-local --client all</code></div>
        <div class="step"><strong>4. Keep it updated</strong><code>agentacct usage watch --interval-seconds 60</code></div>
      </div>
      <div class="subsection-title">Debug JSON endpoints</div>
      <p class="section-note">Every GET rebuilds the work ledger from the store — no caching, by design (zero staleness); the measured envelope is fine to roughly 5,000 saved rows, revisit if stores grow beyond that.</p>
      <div class="table-wrap"><table><thead><tr><th>Endpoint</th><th>Purpose</th></tr></thead><tbody>{debug_endpoint_rows}</tbody></table></div>
      {refused_recording_html}
      <div class="subsection-title">Work Timeline</div>
      <div class="section-header"><h2>Work Timeline</h2><div class="sort-controls">View: {timeline_view_control}</div></div>
      <p class="section-note">Chronological mix of usage, MCP work, evidence, diagnostics, and proxy events. The default Grouped view collapses usage import rows into one entry per agent and day (counts and exact sums only — grouping never allocates usage to work); Everything shows the raw row-level mix.</p>
      {timeline_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Time</th><th>Source</th><th>Event</th><th>Tokens</th><th>Cost</th><th>Confidence / status</th><th>Join confidence</th></tr></thead><tbody>{timeline_rows}</tbody></table></div>
      <div class="subsection-title">Agent source discovery</div>
      <div class="section-header"><h2>Agents / Clients</h2><div class="sort-controls">Sort: {tools_sort_control}</div></div>
      <p class="section-note">Read-only local scan source list and checked paths.</p>
      <div class="subsection-title">AI tools found on this Mac</div>
      <div class="table-wrap"><table><thead><tr><th>Agent</th><th>Status</th><th>Usage records</th><th>Tokens found</th><th>Cost shown</th><th>Provider / model</th><th>Confidence</th><th>Saved</th><th>Local data</th></tr></thead><tbody>{ready_source_rows}</tbody></table></div>
      <div class="subsection-title">Known usage sources not detected</div>
      <p class="section-note">This is runtime source detection, not an agent support matrix. See <a href="/advanced#agent-capability-coverage">Agent capability coverage</a> for per-lane implementation and verification truth.</p>
      <div class="table-wrap"><table><thead><tr><th>Agent</th><th>Status</th><th>Looking for</th><th>Checked paths</th></tr></thead><tbody>{missing_source_rows}</tbody></table></div>
      <div class="subsection-title">Cost and provider breakdown</div>
      <div class="split-grid">
        <div class="inline-panel"><div class="inline-title">Over Time</div><div class="table-wrap"><table><thead><tr><th>Date</th><th>Records</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>{usage_over_time_table_rows}</tbody></table></div>{usage_over_time_cap_note}</div>
        <div class="inline-panel"><div class="inline-title">Cost Confidence Breakdown</div><div class="table-wrap"><table><thead><tr><th>Confidence</th><th>Rows</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>{cost_confidence_table_rows}</tbody></table></div></div>
      </div>
      <div class="subsection-title">Estimated cost breakdown</div>
      <div class="table-wrap"><table><thead><tr><th>Agent</th><th>Provider / model</th><th>Usage records</th><th>Input tokens / cost</th><th>Output tokens / cost</th><th>Cache create / cost</th><th>Cache read / cost</th><th>Total tokens / cost</th></tr></thead><tbody>{cost_breakdown_rows}</tbody></table></div>
      <div class="subsection-title">Imported usage by provider</div>
      <div class="table-wrap"><table><thead><tr><th>Provider</th><th>Records</th><th>Input</th><th>Output</th><th>Total</th><th>Cache create</th><th>Cache read</th><th>Reasoning output</th><th>Total incl. cached</th><th>Cost</th></tr></thead><tbody>{provider_token_rows}</tbody></table></div>
      <div class="subsection-title">Raw diagnostic context bridge</div>
      <div class="section-header"><h2>Raw diagnostic context bridge</h2></div>
      <p class="section-note">Diagnostic view of raw imported usage and MCP context identifiers. Product reconciliation above uses the work ledger join inspector and usage reconciliation state machine as the canonical attribution model.</p>
      <div class="bridge-overview">
        <div class="bridge-stat"><div class="label">Raw usage records</div><div class="value">{esc(_fmt_int(context_bridge.get('usage_records', 0)))}</div><div class="note">Imported model_usage rows</div></div>
        <div class="bridge-stat"><div class="label">Diagnostic context matches</div><div class="value">{esc(_fmt_int(context_bridge.get('context_matched_usage_records', 0)))}</div><div class="note">Identifier matches before canonical reconciliation</div></div>
        <div class="bridge-stat"><div class="label">Diagnostic attributed rows</div><div class="value">{esc(_fmt_int(context_bridge.get('attributed_usage_records', 0)))}</div><div class="note">Legacy diagnostic count, not a separate Product total</div></div>
        <div class="bridge-stat"><div class="label">Context match coverage</div><div class="value">{esc(_fmt_coverage_ratio(context_bridge.get('context_match_coverage_ratio')))}</div><div class="note">all saved usage rows, not only rows shown below</div></div>
        <div class="bridge-stat"><div class="label">Attribution coverage</div><div class="value">{esc(_fmt_coverage_ratio(context_bridge.get('attribution_coverage_ratio')))}</div><div class="note">all saved usage rows attributed to work</div></div>
        <div class="bridge-stat"><div class="label">MCP context events</div><div class="value">{esc(_fmt_int(context_bridge.get('context_events', 0)))}</div><div class="note">Client context, sections, usage-debug</div></div>
        <div class="bridge-stat"><div class="label">Unmatched context</div><div class="value">{esc(_fmt_int(context_bridge.get('unlinked_context_events', 0)))}</div><div class="note">Waiting for a matching usage row</div></div>
      </div>
      {bridge_status_notice}
      {bridge_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Raw usage log</th><th>Tokens / cost truth</th><th>Join evidence</th><th>MCP context</th><th>MCP semantics</th></tr></thead><tbody>{bridge_rows}</tbody></table></div>
      <div class="subsection-title">Unlinked MCP context</div>
      {unlinked_context_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Semantic event</th><th>Agent / session</th><th>Section or basis</th><th>Summary</th></tr></thead><tbody>{unlinked_context_rows}</tbody></table></div>
      <div class="subsection-title">Activity log</div>
      <div class="section-header"><h2>Activity log</h2><div class="sort-controls">Sort: {activity_sort_control}</div></div>
      <p class="section-note">Saved AI usage records, sorted by the client session timestamp when available. Main chats, child agents, and internal auto-review overhead are labeled separately.</p>
      {activity_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Session time</th><th>Agent</th><th>Session</th><th>Provider / model</th><th>Tokens</th><th>Cost</th><th>Confidence</th></tr></thead><tbody>{activity_rows}</tbody></table></div>
      <div class="subsection-title">agentacct event log</div>
      <div class="section-header"><h2>agentacct event log</h2></div>
      <p class="section-note">Workflow checkpoints, MCP notes, and other non-usage records. These are kept separate from AI session usage. agentacct's own diagnostic tool events stay visible here with a self-test label.</p>
      {raw_event_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Recorded</th><th>Source</th><th>Activity</th><th>Provider / model</th><th>Tokens</th><th>Cost</th><th>Confidence</th><th>Run</th></tr></thead><tbody>{raw_event_rows}</tbody></table></div>
      <div class="subsection-title">agentacct-launched commands</div>
      <div class="section-header"><h2>agentacct-launched commands</h2></div>
      <p class="section-note">Latest 20 commands.</p>
      <div class="table-wrap"><table><thead><tr><th>Started</th><th>Status</th><th>Duration</th><th>Command</th><th>Run</th></tr></thead><tbody>{command_rows}</tbody></table></div>
      <div class="subsection-title">API budget decisions</div>
      <div class="section-header"><h2>API budget decisions</h2></div>
      {cost_event_cap_note}
      <div class="table-wrap"><table><thead><tr><th>Time</th><th>Run</th><th>Provider / model</th><th>Decision</th><th>Cost</th><th>Confidence</th></tr></thead><tbody>{cost_event_rows}</tbody></table></div>
    </section>
"""


def _overview_subtitle(data: _DashboardPageData) -> str:
    """The Overview page subtitle. Shared so the v1 and canonical renders of
    the same page stay identically worded."""

    if data.store_label == "All projects":
        return "All projects · What agents are doing across all local projects. Newest activity first; open findings and blockers stay pinned."
    return f"What agents are doing in {data.store_label}. Newest activity first; open findings and blockers stay pinned."


def _render_overview_page(
    data: _DashboardPageData,
    *,
    evidence_product: Mapping[str, Any] | None = None,
    import_status: dict[str, Any] | None = None,
    activation: Mapping[str, Any] | None = None,
    fallback_notice: str = "",
    usage_breakdown: str = "agent",
) -> str:
    return _page_doc(
        page_id="overview",
        identity_html="",
        notice_html=(
            fallback_notice
            + _overview_freshness_html(data, data.esc)
            + _ingestion_health_notice_html(data.ingestion_health, data.esc)
            + _import_notice_html(import_status, data.esc)
        ),
        body_html=_overview_body(
            data,
            evidence_product=evidence_product,
            activation=activation,
            usage_breakdown=usage_breakdown,
        ),
        page_title="Work",
        page_subtitle=_overview_subtitle(data),
    )


def _task_detail_body(payload: Mapping[str, Any], esc: Any) -> str:
    brief = payload.get("decision_brief") if isinstance(payload.get("decision_brief"), Mapping) else {}
    states = payload.get("states") if isinstance(payload.get("states"), Mapping) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), list) else []
    lanes = payload.get("lanes") if isinstance(payload.get("lanes"), list) else []
    timeline = payload.get("timeline") if isinstance(payload.get("timeline"), Mapping) else {}
    findings = payload.get("findings") if isinstance(payload.get("findings"), Mapping) else {}
    current_findings = findings.get("current") if isinstance(findings.get("current"), list) else []
    timeline_events = timeline.get("events") if isinstance(timeline.get("events"), list) else []
    models = [str(model) for model in payload.get("models", []) if model]
    state_cells = "".join(
        '<div class="task-state-cell">'
        f'<small>{esc(dimension)}</small><strong>{esc(value.get("label") if isinstance(value, Mapping) else "Unknown")}</strong>'
        "</div>"
        for dimension, value in states.items()
    )
    strongest = str(brief.get("strongest_proof") or "No machine proof recorded yet.")
    finding = str(brief.get("unresolved_finding") or "No open recorded finding.")
    attention_state = str(brief.get("finding_attention_state") or "")
    if not current_findings:
        finding_label = "Current finding"
        finding_html = f"<p>{esc(finding)}</p>"
    else:
        finding_states = {
            str(row.get("attention_state") or "open")
            for row in current_findings
            if isinstance(row, Mapping)
        }
        finding_label = (
            "Open finding"
            if "open" in finding_states
            else "Current findings"
            if len(finding_states) > 1
            else "Reviewed finding"
            if "reviewed" in finding_states
            else "Marked resolved finding"
        )
        finding_rows: list[str] = []
        for row in current_findings:
            if not isinstance(row, Mapping):
                continue
            row_state = str(row.get("attention_state") or "open")
            state_label = {
                "reviewed": "Reviewed",
                "resolved": "Marked resolved",
            }.get(row_state, "Open")
            state_css = "status-finding" if row_state == "open" else "status-missing"
            truth = (
                "Reviewed by you; no passing check has replaced this failed evidence."
                if row_state == "reviewed"
                else "Marked resolved by you; this is not machine verification."
                if row_state == "resolved"
                else "The failed/error result remains current objective evidence."
            )
            note = row.get("disposition") if isinstance(row.get("disposition"), Mapping) else {}
            note_text = str(note.get("note") or "")
            finding_rows.append(
                '<div class="task-detail-finding">'
                f'<span class="status {esc(state_css)}">{esc(state_label)}</span>'
                f'<p>{esc(row.get("summary") or "Recorded finding")}</p>'
                f'<small>{esc(truth)}</small>'
                + (f'<small>Disposition note: {esc(note_text)}</small>' if note_text else "")
                + "</div>"
            )
        finding_html = "".join(finding_rows)
        if attention_state in {"reviewed", "resolved"}:
            finding_html += (
                '<p class="note"><a href="/#work-feed">Return to the Work card</a> to reopen or update attention.</p>'
            )
    owner = str(brief.get("owner") or "No owner recorded")
    next_action = str(brief.get("next_action") or "agentacct does not invent the next action.")
    excluded_rows = int(usage.get("excluded_non_additive_rows") or 0)
    known_additive_cost = usage.get("known_additive_cost_usd")
    cost_text = (
        _fmt_optional_usd_text(usage.get("estimated_cost_usd")) + " est."
        if usage.get("estimated_cost_usd") is not None
        else _fmt_optional_usd_text(known_additive_cost) + " known subtotal; complete cost unavailable"
        if excluded_rows and known_additive_cost is not None
        else "Unavailable"
    )
    usage_basis = str(usage.get("usage_confidence") or "unknown").replace("_", " ").capitalize()
    cost_basis = str(usage.get("cost_confidence") or "unknown").replace("_", " ").capitalize()
    duration = _fmt_duration_hm(payload.get("duration_seconds")) or "Unavailable"
    additive_rows = int((usage.get("additive_rows") if "additive_rows" in usage else usage.get("rows")) or 0)
    facts = [
        ("Models", " · ".join(models) if models else "Not recorded"),
        ("Task span", duration),
        (
            "Known additive subtotal incl. cache" if excluded_rows else "Total incl. cache",
            _fmt_compact_int(usage.get("total_tokens"))
            if additive_rows
            else "Unavailable",
        ),
        (
            "Known input + output subtotal" if excluded_rows else "Input + output",
            _fmt_compact_int(usage.get("fresh_tokens"))
            if additive_rows
            else "Unavailable",
        ),
        (
            "Known cache-read subtotal" if excluded_rows else "Cache reads",
            _fmt_compact_int(usage.get("cache_read_tokens"))
            if additive_rows
            else "Unavailable",
        ),
        (
            "Usage held",
            f"{_fmt_int(excluded_rows)} rows · awaiting source identity or lineage normalization"
            if excluded_rows
            else "None",
        ),
        ("Usage basis", usage_basis),
        ("Cost", f"{cost_text} · {cost_basis}"),
        ("Evidence shown", f"{_fmt_int(timeline.get('shown') or 0)} of {_fmt_int(timeline.get('total') or 0)} milestones"),
    ]
    fact_html = "".join(
        f'<div class="task-fact"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>'
        for label, value in facts
    )
    coverage_html = "".join(
        '<div class="coverage-card">'
        f'<strong>{esc(row.get("dimension") if isinstance(row, Mapping) else "unknown")}</strong>'
        f'<span class="status status-missing">{esc(str(row.get("state") if isinstance(row, Mapping) else "unknown").replace("_", " ").title())}</span>'
        f'<small>{esc(row.get("source") if isinstance(row, Mapping) and row.get("source") else "No source recorded")}</small>'
        "</div>"
        for row in coverage
    )
    lane_summary = " · ".join(
        f'{str(row.get("role_label") or row.get("role") or "lane")}: {_human_client(row.get("client"))}'
        for row in lanes
        if isinstance(row, Mapping)
    ) or "No session lanes recorded"
    lane_html = "".join(
        '<div class="task-lane">'
        f'<strong>{esc(row.get("role_label") or row.get("role") or "Lane")}</strong>'
        f'<span>{esc(_human_client(row.get("client")))}'
        f'{esc(" · " + " / ".join(str(model) for model in row.get("models", []) if model)) if row.get("models") else ""}</span>'
        "</div>"
        for row in lanes
        if isinstance(row, Mapping)
    )
    timeline_rows: list[str] = []
    for event in timeline_events:
        if not isinstance(event, Mapping):
            continue
        occurred_at = float(event.get("occurred_at") or 0.0)
        when = _fmt_time(occurred_at) if occurred_at else "Time not recorded"
        source = str(event.get("source") or "Source not recorded")
        confidence = str(event.get("confidence") or "unknown")
        timeline_rows.append(
            '<li class="task-timeline-row">'
            f'<div class="task-timeline-meta">{esc(when)}<br>{esc(source)} · {esc(confidence)}</div>'
            f'<div><div class="task-timeline-title">{esc(event.get("title") or "Recorded milestone")}</div>'
            f'<small>{esc(str(event.get("kind") or "event").replace("_", " ").title())} · {esc(str(event.get("status") or "recorded").replace("_", " ").title())}</small></div>'
            f'<span class="task-lane-chip">{esc(str(event.get("lane") or "task").replace("_", " ").title())}</span>'
            "</li>"
        )
    truncation = (
        f'<p class="section-note">Showing {_fmt_int(timeline.get("shown") or 0)} of {_fmt_int(timeline.get("total") or 0)} meaningful milestones. Latest checks, findings, and control actions are retained.</p>'
        if timeline.get("truncated")
        else ""
    )
    raw = payload.get("raw_evidence") if isinstance(payload.get("raw_evidence"), Mapping) else {}
    # Redacted evidence inventory: a per-item list built from the already-redacted
    # timeline events (work records + checks), grouped by kind, so expanding the
    # inventory shows an ACTUAL list — not just totals. Session rows stay a count
    # only: their raw client_session_ids remain forensic-API-only, never here.
    inventory_kind_labels = {
        "work": "Work records",
        "check": "Checks",
        "attempt": "Control attempts",
        "control": "Control events",
    }
    inventory_by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for event in timeline_events:
        if isinstance(event, Mapping):
            inventory_by_kind.setdefault(str(event.get("kind") or "event"), []).append(event)
    _inventory_order = ["work", "check", "attempt", "control"]
    inventory_blocks: list[str] = []
    for kind_key in _inventory_order + sorted(set(inventory_by_kind) - set(_inventory_order)):
        events = inventory_by_kind.get(kind_key)
        if not events:
            continue
        label = inventory_kind_labels.get(kind_key, str(kind_key).replace("_", " ").title())
        item_rows = "".join(
            '<li class="evidence-inventory-row">'
            f'<div><strong>{esc(event.get("title") or "Recorded item")}</strong>'
            f'<small>{esc(str(event.get("status") or "recorded").replace("_", " ").title())}'
            f'{esc(" · " + _fmt_time(event.get("occurred_at"))) if _fmt_time(event.get("occurred_at")) else ""}'
            f'{esc(" · " + str(event.get("source"))) if event.get("source") else ""}</small></div>'
            "</li>"
            for event in sorted(events, key=lambda e: -float(e.get("occurred_at") or 0.0))
        )
        inventory_blocks.append(
            f'<div class="run-detail-block"><strong>{esc(label)} ({esc(_fmt_int(len(events)))})</strong>'
            f'<ul class="evidence-inventory-list">{item_rows}</ul></div>'
        )
    inventory_items_html = "".join(inventory_blocks) or (
        '<p class="section-note">No itemized work records or checks were recorded for this Task.</p>'
    )
    return f"""
    <a class="task-detail-back" href="/#work-feed">&larr; Back to Work</a>
    <section class="task-brief" aria-labelledby="decision-brief-title">
      <div><div class="eyebrow">Decision brief</div><h2 id="decision-brief-title">{esc(brief.get("outcome_statement") or "No outcome recorded.")}</h2></div>
      <div class="task-state-axis" aria-label="Task state axes">{state_cells}</div>
      <div class="task-lane-strip" aria-label="Agent and model lanes">{lane_html or '<span class="note">No agent/model lane was recorded.</span>'}</div>
      <div class="task-brief-proof">
        <div class="task-brief-callout"><strong>Strongest proof</strong><p>{esc(strongest)}</p></div>
        <div class="task-brief-callout"><strong>{esc(finding_label)}</strong>{finding_html}</div>
        <div class="task-brief-callout"><strong>Action owner</strong><p>{esc(owner)}</p></div>
        <div class="task-brief-callout"><strong>Next action</strong><p>{esc(next_action)}</p></div>
      </div>
      <div class="task-facts">{fact_html}</div>
    </section>
    <section class="task-detail-section" aria-labelledby="coverage-title"><h2 id="coverage-title">What agentacct can prove</h2><div class="coverage-grid">{coverage_html}</div></section>
    <section class="task-detail-section" aria-labelledby="timeline-title"><h2 id="timeline-title">Execution timeline</h2><p class="section-note">{esc(lane_summary)}. Milestones are chronological; raw turns and telemetry stay collapsed.</p>{truncation}<ol class="task-timeline">{"".join(timeline_rows) or '<li class="empty">No meaningful milestone was recorded.</li>'}</ol></section>
    <section class="task-detail-section"><details><summary>Show evidence inventory</summary><p class="section-note">{esc(_fmt_int(raw.get("session_count") or 0))} sessions · {esc(_fmt_int(raw.get("work_item_count") or 0))} work records · {esc(_fmt_int(raw.get("check_count") or 0))} checks.</p><div class="evidence-inventory">{inventory_items_html}</div><p class="section-note">Each row is redacted to type, result, and time. Raw session and transcript identifiers stay in the local forensic API, not this public Task URL.</p></details></section>
    """


def _render_task_detail_page(payload: Mapping[str, Any], *, fallback_notice: str = "") -> str:
    return _page_doc(
        page_id="task-detail",
        identity_html="",
        notice_html=fallback_notice,
        body_html=_task_detail_body(payload, _esc_html),
        page_title=str(payload.get("title") or "Task"),
        page_subtitle="One evidence-backed view of what happened, what proves it, and what remains unresolved.",
        footer_html="agentacct Task Intelligence &mdash; localhost only. Missing evidence stays missing; usage is never allocated to make a Task look complete.",
    )


def _render_tokens_page(
    data: _DashboardPageData,
    *,
    client: str = "all",
    model: str = "all",
    days: str = "30",
    granularity: str = "auto",
    cost_sort: str = "total",
    fallback_notice: str = "",
) -> str:
    return _page_doc(
        page_id="tokens",
        identity_html=data.identity_html,
        notice_html=fallback_notice,
        body_html=_tokens_body(
            data, client=client, model=model, days=days, granularity=granularity, cost_sort=cost_sort
        ),
    )


def _render_sessions_page(
    data: _DashboardPageData,
    *,
    client: str = "all",
    project: str = "all",
    join: str = "all",
    kind: str = "grouped",
    days: str = "30",
    sort: str = "recent",
    work: str = "all",
    show: int = SESSION_ROLLUP_DISPLAY_LIMIT,
    fallback_notice: str = "",
) -> str:
    return _page_doc(
        page_id="sessions",
        identity_html=data.identity_html,
        notice_html=fallback_notice,
        body_html=_sessions_body(
            data, client=client, project=project, join=join, kind=kind, days=days,
            sort=sort, work=work, show=show,
        ),
    )


def _render_raw_page(
    data: _DashboardPageData,
    *,
    runs: list[dict[str, Any]],
    usage_sources: list[UsageSourceDiscovery],
    tools_sort: str = "tokens",
    cost_sort: str = "total",
    activity_sort: str = "newest",
    timeline_view: str = "grouped",
) -> str:
    return _page_doc(
        page_id="raw",
        identity_html=data.identity_html,
        body_html=_raw_body(
            data,
            runs=runs,
            usage_sources=usage_sources,
            tools_sort=tools_sort,
            cost_sort=cost_sort,
            activity_sort=activity_sort,
            timeline_view=timeline_view,
        ),
    )


# --- canonical read-mode payloads (migration phase 4.2) ---------------------
#
# Work/Task/Sessions JSON surfaces served from the canonical store when the
# read flag is on. The canonical model deliberately represents a SUBSET of v1
# (work claims, sessions, lineage, usage): every field it cannot represent is
# absent or None here with the gap NAMED in ``model_gaps`` — never synthesized
# from v1, never coerced to 0.

CANONICAL_TASK_PROJECTION_SCHEMA_VERSION = "agent-chronicle.task-projection.v2-canonical"
CANONICAL_TASK_INTELLIGENCE_SCHEMA_VERSION = "agent-chronicle.task-intelligence.v2-canonical"
CANONICAL_SESSION_ROLLUP_SCHEMA_VERSION = "agent-sentinel.session-rollup.v2-canonical"

# Whitelisted staleness label for projection-backed canonical surfaces (the
# 4.1 usage surface pins the same keys).
_CANONICAL_PROJECTION_LABEL_KEYS = (
    "projection_name",
    "projection_version",
    "built_through_sequence",
    "canonical_sequence",
    "state",
    "built_at_us",
    "stale",
    "pending_writes",
)

_CANONICAL_TASK_MODEL_GAPS: dict[str, Any] = {
    "not_represented": [
        # No canonical writer records findings at all today (the shadow
        # writer skips finding lanes visibly), so the whole finding surface —
        # inventory AND dispositions — is a gap, and rm_task_current's
        # finding_count (a count over a table nothing populates) is
        # deliberately NOT served: its 0 would impersonate "no findings".
        "findings",
        "finding_dispositions",
        "continuation_memberships",
        "planned_control_tasks",
        "machine_check_evidence",
        "run_reports",
        "generic_notes",
        "work_item_metadata",
        "estimated_cost",
        "usage_confidence_provenance",
        "per_session_breakdown",
        "project_attribution",
    ],
    "reason": (
        "the canonical model represents work claims, sessions, lineage, and "
        "usage; fields it cannot represent are absent or None here — never "
        "synthesized from the v1 ledger"
    ),
    "projected_state_basis": (
        "latest non-superseded work claim; reported_complete is a claim, "
        "not a verification"
    ),
}

_CANONICAL_SESSION_MODEL_GAPS: dict[str, Any] = {
    "not_represented": [
        "usage_rollup",
        "estimated_cost",
        "join_attribution",
        "work_associations",
        "project_attribution",
        "session_titles",
        "instrumentation_state",
        "mechanical_capture_detail",
        "child_session_rollups",
    ],
    "not_served": [
        # In the model but pre-validation state: the raw observed-lineage
        # claim is deliberately withheld; only VALIDATED parent edges serve.
        "observed_unvalidated_lineage",
    ],
    "reason": (
        "the canonical model represents work claims, sessions, lineage, and "
        "usage; fields it cannot represent are absent or None here — never "
        "synthesized from the v1 ledger"
    ),
}

# The Overview page composes three canonical reads (tasks, usage, sessions).
# Its honest bounded slice caps each list; the source note labels truncation.
_CANONICAL_OVERVIEW_TASK_ROWS = 25
_CANONICAL_OVERVIEW_SESSION_ROWS = 25

# The Overview's model-gaps note is the UNION of the task and session gaps
# (usage labels its own exclusions inline in the day cube). Computed from the
# sub-constants so it can never drift out of sync with them.
_CANONICAL_OVERVIEW_MODEL_GAPS: dict[str, Any] = {
    "not_represented": list(
        dict.fromkeys(
            [
                *_CANONICAL_TASK_MODEL_GAPS["not_represented"],
                *_CANONICAL_SESSION_MODEL_GAPS["not_represented"],
            ]
        )
    ),
    "reason": (
        "the Overview composes the canonical task, usage, and session read "
        "models; fields none of them represent are absent or None here — never "
        "synthesized from the v1 ledger"
    ),
}


def _canonical_us_iso(value: int | None) -> str | None:
    """Microsecond epoch → UTC ISO-8601, exactly (no float rounding)."""

    if value is None:
        return None
    base = datetime.fromtimestamp(int(value) // 1_000_000, tz=timezone.utc)
    return (base + timedelta(microseconds=int(value) % 1_000_000)).isoformat()


def _canonical_fallback_label(failure: CanonicalReadUnavailable) -> dict[str, Any]:
    return {
        "active": False,
        "source": "v1_fallback",
        "reason": failure.reason,
        "detail": failure.detail,
    }


def _canonical_projection_label(projection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: projection[key] for key in _CANONICAL_PROJECTION_LABEL_KEYS if key in projection
    }


def _canonical_session_identity(
    session: Any, sources: Mapping[int, Any]
) -> dict[str, Any]:
    """Presentation identity for one canonical session row. An unresolvable
    source keeps ``client`` None and says so — never guessed."""

    source = sources.get(session.source_instance_id)
    return {
        "canonical_session_id": session.session_id,
        "client": source.client if source is not None else None,
        "client_session_id": session.client_session_id,
        "session_kind": session.session_kind,
        "representation": source.representation if source is not None else None,
        "client_identity_resolved": source is not None,
    }


def _canonical_task_entry(
    task: Any, sessions: Mapping[int, Any], sources: Mapping[int, Any]
) -> dict[str, Any]:
    primary = sessions.get(task.primary_session_id)
    return {
        "public_task_id": task.public_task_id,
        "title": task.title,
        "projected_state": task.projected_state,
        "last_activity_at": _canonical_us_iso(task.last_activity_at_us),
        "last_activity_at_us": task.last_activity_at_us,
        "session_count": task.session_count,
        "usage_measurement_count": task.usage_measurement_count,
        # Token fields are the projection's honest nulls: None means "not
        # reported", never 0.
        "input_tokens": task.input_tokens,
        "output_tokens": task.output_tokens,
        "total_tokens": task.total_tokens,
        "usage_missing_field_count": task.usage_missing_field_count,
        # rm_task_current.finding_count is intentionally absent: no canonical
        # writer populates finding_actions yet, so serving its 0 would let
        # missing data impersonate "no findings" (see model_gaps).
        "primary_session": (
            _canonical_session_identity(primary, sources) if primary is not None else None
        ),
    }


def _canonical_task_projection_payload(read: CanonicalTaskListRead) -> dict[str, Any]:
    return {
        "schema_version": CANONICAL_TASK_PROJECTION_SCHEMA_VERSION,
        "canonical_read": {
            "active": True,
            "source": "canonical",
            "store": dict(read.store),
            "projection": _canonical_projection_label(read.projection),
            "truncated": bool(read.truncated),
        },
        "summary": {
            "task_count_shown": len(read.tasks),
            "truncated": bool(read.truncated),
        },
        "tasks": [
            _canonical_task_entry(task, read.sessions, read.sources) for task in read.tasks
        ],
        "model_gaps": _CANONICAL_TASK_MODEL_GAPS,
    }


def _canonical_task_intelligence_payload(read: CanonicalTaskDetailRead) -> dict[str, Any]:
    if read.task is None:
        raise ValueError("canonical task detail payload requires a resolved task")
    return {
        "schema_version": CANONICAL_TASK_INTELLIGENCE_SCHEMA_VERSION,
        "canonical_read": {
            "active": True,
            "source": "canonical",
            "store": dict(read.store),
            "projection": _canonical_projection_label(read.projection),
        },
        "task": _canonical_task_entry(read.task, read.sessions, read.sources),
        "model_gaps": _CANONICAL_TASK_MODEL_GAPS,
    }


def _canonical_session_entry(
    session: Any, read: CanonicalSessionListRead, page_sessions: Mapping[int, Any]
) -> dict[str, Any]:
    entry = _canonical_session_identity(session, read.sources)
    source = read.sources.get(session.source_instance_id)
    entry["namespace_scheme"] = source.namespace_scheme if source is not None else None
    entry["started_at"] = _canonical_us_iso(session.started_at_us)
    entry["started_at_us"] = session.started_at_us
    entry["last_activity_at"] = _canonical_us_iso(session.last_activity_at_us)
    entry["last_activity_at_us"] = session.last_activity_at_us
    edge = read.lineage_edges.get(session.session_id)
    if edge is None:
        entry["parent"] = None
    else:
        parent_id, relation = edge
        parent = page_sessions.get(parent_id) or read.parent_sessions.get(parent_id)
        if parent is None:
            # The edge names a parent the lookups could not resolve; say so
            # rather than dropping the validated relation.
            entry["parent"] = {
                "canonical_session_id": parent_id,
                "relation": relation,
                "client": None,
                "client_session_id": None,
                "identity_resolved": False,
            }
        else:
            entry["parent"] = {
                "canonical_session_id": parent_id,
                "relation": relation,
                "client": (
                    read.sources[parent.source_instance_id].client
                    if parent.source_instance_id in read.sources
                    else None
                ),
                "client_session_id": parent.client_session_id,
                "identity_resolved": parent.source_instance_id in read.sources,
            }
    return entry


def _canonical_session_rollup_payload(read: CanonicalSessionListRead) -> dict[str, Any]:
    page_sessions = {session.session_id: session for session in read.sessions}
    return {
        "schema_version": CANONICAL_SESSION_ROLLUP_SCHEMA_VERSION,
        "canonical_read": {
            "active": True,
            "source": "canonical",
            "store": dict(read.store),
            "truncated": bool(read.truncated),
        },
        # The full-store count is not computed on this path; None means "not
        # computed", never 0 (the shown count is exact for the page).
        "total_sessions": None,
        "total_sessions_reason": "not_computed_in_canonical_read",
        "session_count_shown": len(read.sessions),
        "sessions": [
            _canonical_session_entry(session, read, page_sessions)
            for session in read.sessions
        ],
        "model_gaps": _CANONICAL_SESSION_MODEL_GAPS,
    }


# --- canonical read-mode HTML bodies (migration phase 4.4 / P1) -------------
#
# The HTML analog of the phase-4.1/4.2 canonical JSON payload builders above.
# When the read flag is on and the canonical store serves, these render the
# HONEST SUBSET the canonical model represents — never the v1 ledger, never a
# synthesized field. Everything the model cannot represent is named in the
# model-gaps note; the projection staleness and store identity ride visible in
# the source note. When the flag is on but the store cannot serve, the handler
# renders the v1 body under the VISIBLE fallback banner below instead (locked
# principle: any v1 fallback must be labeled; silent wrong data is forbidden).


def _canonical_uuid8(store: Mapping[str, Any] | None) -> str:
    return str((store or {}).get("store_uuid") or "")[:8]


def _canonical_count_cell(value: Any) -> str:
    """Honest count for a table cell: None (not reported) stays &mdash;, never 0."""

    return "&mdash;" if value is None else _fmt_int(value)


def _canonical_model_gaps_note(model_gaps: Mapping[str, Any] | None, esc: Any) -> str:
    names = list((model_gaps or {}).get("not_represented") or [])
    if not names:
        return ""
    listed = ", ".join(esc(name) for name in names)
    return (
        '<p class="note">Not represented in the canonical model: '
        f"{listed}. These fields are absent by design and are never "
        "synthesized from the v1 ledger.</p>"
    )


def _canonical_fallback_notice_html(
    failure: CanonicalReadUnavailable, *, surface: str, esc: Any
) -> str:
    """The VISIBLE labeled v1 fallback banner.

    Rendered only when the read flag is on and the canonical store could not
    honestly serve. The reason/detail come straight from the typed failure so
    the page never silently shows v1 as if it were canonical.
    """

    detail = f" &mdash; {esc(failure.detail)}" if failure.detail else ""
    return (
        '<div class="surface canonical-fallback" role="alert" '
        'data-canonical-read="v1_fallback" '
        f'data-canonical-surface="{esc(surface)}" '
        f'data-canonical-reason="{esc(failure.reason)}">'
        "<strong>Showing v1 fallback data</strong>"
        f'<p class="note">The canonical store could not serve the '
        f"{esc(surface)} surface ({esc(failure.reason)}){detail}. This page is "
        "the v1 model, shown here and labeled — never presented as canonical.</p>"
        "</div>"
    )


def _canonical_source_note_html(
    *,
    surface: str,
    store: Mapping[str, Any],
    projection: Mapping[str, Any] | None,
    truncated: bool,
    model_gaps: Mapping[str, Any] | None,
    esc: Any,
    extra_html: str = "",
) -> str:
    """The "served from canonical" indicator for a migrated HTML surface.

    Carries the same evidence the JSON ``canonical_read`` label carries —
    store identity, projection staleness, truncation — so a reader can see the
    page is canonical (not v1) and how fresh the read model is.
    """

    uuid8 = _canonical_uuid8(store)
    bits = [f"store {esc(uuid8)}" if uuid8 else "store &mdash;"]
    role = str((store or {}).get("store_role") or "")
    if role:
        bits.append(f"role {esc(role)}")
    if projection is not None:
        state = str(projection.get("state") or "")
        pending = projection.get("pending_writes")
        stale = bool(projection.get("stale"))
        bits.append(f"projection {esc(state)}")
        if stale:
            pending_text = _fmt_int(pending) if isinstance(pending, int) else esc(pending)
            bits.append(f"stale ({pending_text} pending write(s))")
        else:
            bits.append("current")
    if truncated:
        bits.append("page truncated — showing a bounded newest-first slice")
    meta = " · ".join(bits)
    return (
        '<div class="surface canonical-source" role="status" '
        'data-canonical-read="canonical" '
        f'data-canonical-surface="{esc(surface)}">'
        "<strong>Served from the canonical store</strong>"
        f'<p class="note">{meta}</p>'
        f"{extra_html}"
        f"{_canonical_model_gaps_note(model_gaps, esc)}"
        "</div>"
    )


def _canonical_sessions_body(
    read: CanonicalSessionListRead, *, esc: Any
) -> str:
    """Honest-subset canonical Sessions table (newest-first store page)."""

    page_sessions = {session.session_id: session for session in read.sessions}
    rows_html: list[str] = []
    for session in read.sessions:
        entry = _canonical_session_entry(session, read, page_sessions)
        client = entry.get("client")
        client_html = esc(client) if client else '<span class="note">unresolved</span>'
        cid = str(entry.get("client_session_id") or "")
        cid_display = cid if len(cid) <= 20 else "…" + cid[-16:]
        parent = entry.get("parent")
        if not parent:
            parent_html = "&mdash;"
        else:
            parent_cid = str(parent.get("client_session_id") or "")
            parent_short = (
                parent_cid if len(parent_cid) <= 16 else "…" + parent_cid[-12:]
            ) if parent_cid else f"#{esc(parent.get('canonical_session_id'))}"
            parent_html = f"{esc(parent.get('relation'))} · <code>{esc(parent_short)}</code>"
            if not parent.get("identity_resolved"):
                parent_html += ' <span class="note">(unresolved)</span>'
        rows_html.append(
            "<tr>"
            f"<td>{client_html}</td>"
            f'<td><code title="{esc(cid)}">{esc(cid_display)}</code></td>'
            f"<td>{esc(entry.get('session_kind'))}</td>"
            f"<td>{esc(entry.get('started_at')) or '&mdash;'}</td>"
            f"<td>{esc(entry.get('last_activity_at')) or '&mdash;'}</td>"
            f"<td>{parent_html}</td>"
            "</tr>"
        )
    if rows_html:
        table = (
            '<div class="table-wrap"><table><thead><tr>'
            "<th>Client</th><th>Session</th><th>Kind</th>"
            "<th>Started</th><th>Last activity</th><th>Parent (validated)</th>"
            "</tr></thead><tbody>"
            + "".join(rows_html)
            + "</tbody></table></div>"
        )
    else:
        table = '<p class="empty-state">No sessions in the canonical store yet.</p>'
    return (
        '<section class="surface">'
        f"<h2>Sessions <span class=\"note\">({_fmt_int(len(read.sessions))} shown)</span></h2>"
        f"{table}"
        "</section>"
    )


def _canonical_sessions_filters_note(
    *, client: str, project: str, join: str, kind: str, days: str, esc: Any
) -> str:
    """Name the v1 explorer filter params that the base-fact canonical read
    cannot honor, mirroring the JSON surface's ``ignored_html_params`` honesty.
    """

    requested = [
        (name, value)
        for name, value, default in (
            ("client", client, "all"),
            ("project", project, "all"),
            ("join", join, "all"),
            ("kind", kind, "grouped"),
            ("days", days, "30"),
        )
        if value != default
    ]
    if not requested:
        return (
            '<p class="note">The client/project/join/kind/days filters apply to '
            "the v1 explorer only and are not applied to this canonical page.</p>"
        )
    named = ", ".join(f"{esc(name)}={esc(value)}" for name, value in requested)
    return (
        '<p class="note">Filters not applied in canonical mode (v1 explorer '
        f"only): {named}. The canonical page is the newest-first store slice.</p>"
    )


def _render_canonical_sessions_page(
    read: CanonicalSessionListRead,
    *,
    client: str,
    project: str,
    join: str,
    kind: str,
    days: str,
) -> str:
    filters_note = _canonical_sessions_filters_note(
        client=client, project=project, join=join, kind=kind, days=days, esc=_esc_html
    )
    return _page_doc(
        page_id="sessions",
        identity_html="",
        notice_html=_canonical_source_note_html(
            surface="session_list",
            store=read.store,
            projection=None,
            truncated=read.truncated,
            model_gaps=_CANONICAL_SESSION_MODEL_GAPS,
            esc=_esc_html,
            extra_html=filters_note,
        ),
        body_html=_canonical_sessions_body(read, esc=_esc_html),
    )


def _canonical_usage_bucket_cells(bucket: Mapping[str, Any]) -> str:
    """The shared trailing cells for one finalized usage bucket."""

    return (
        f"<td>{_canonical_count_cell(bucket.get('measurement_days'))}</td>"
        f"<td>{_canonical_count_cell(bucket.get('fresh_tokens'))}</td>"
        f"<td>{_canonical_count_cell(bucket.get('total_tokens_including_cached'))}</td>"
        f"<td>{_fmt_usd(bucket.get('estimated_cost_usd')) if bucket.get('estimated_cost_usd') is not None else '&mdash;'}</td>"
        f"<td>{_esc_html(bucket.get('usage_availability'))}</td>"
    )


def _canonical_tokens_body(
    read: CanonicalUsageDayRead,
    *,
    client: str,
    model: str,
    days: str,
    effective_granularity: str,
    days_int: int | None,
    today_utc: date,
    esc: Any,
) -> str:
    """Honest-subset canonical Tokens/usage view built from rm_usage_day."""

    cube = build_canonical_day_cube(
        read.rows,
        read.undated_rows,
        granularity=effective_granularity,
        days=days_int,
        today=today_utc,
    )
    totals = cube["totals"]
    metric_grid = (
        '<div class="metric-grid">'
        f'<div class="metric"><span class="note">Measurement days</span>'
        f"<strong>{_canonical_count_cell(totals.get('measurement_days'))}</strong></div>"
        f'<div class="metric"><span class="note">Fresh tokens</span>'
        f"<strong>{_canonical_count_cell(totals.get('fresh_tokens'))}</strong></div>"
        f'<div class="metric"><span class="note">Total (incl. cached)</span>'
        f"<strong>{_canonical_count_cell(totals.get('total_tokens_including_cached'))}</strong></div>"
        f'<div class="metric"><span class="note">Estimated cost ({esc(totals.get("cost_completeness"))})</span>'
        f"<strong>{_fmt_usd(totals.get('estimated_cost_usd')) if totals.get('estimated_cost_usd') is not None else '&mdash;'}</strong></div>"
        "</div>"
    )

    def _table(title: str, header_html: str, rows: list[str]) -> str:
        if not rows:
            return (
                f"<h2>{esc(title)}</h2>"
                '<p class="empty-state">No rows in this range.</p>'
            )
        return (
            f"<h2>{esc(title)}</h2>"
            '<div class="table-wrap"><table><thead><tr>'
            f"{header_html}"
            "<th>Meas. days</th><th>Fresh tokens</th><th>Total (incl. cached)</th>"
            "<th>Est. cost</th><th>Availability</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        )

    by_client_rows = [
        f"<tr><td>{esc(row.get('client'))}</td>{_canonical_usage_bucket_cells(row)}</tr>"
        for row in cube["by_client"]
    ]
    by_model_rows = []
    for row in cube["by_model"]:
        model_name = row.get("model")
        model_cell = esc(model_name) if model_name else '<span class="note">unknown</span>'
        by_model_rows.append(
            "<tr>"
            f"<td>{esc(row.get('client'))}</td>"
            f"<td>{model_cell}</td>"
            f"{_canonical_usage_bucket_cells(row)}</tr>"
        )
    by_period_rows = [
        f"<tr><td>{esc(row.get('period'))}</td>{_canonical_usage_bucket_cells(row)}</tr>"
        for row in cube["by_period"]
    ]

    held = totals.get("held_measurement_days") or 0
    undated = cube["undated"]
    undated_days = undated.get("measurement_days") or 0
    exclusions: list[str] = []
    if held:
        exclusions.append(
            f"{_fmt_int(held)} held measurement day(s) are non-additive usage the "
            "canonical model refuses to sum"
        )
    if undated_days:
        included = "included in totals" if undated.get("included_in_totals") else "excluded from a bounded range"
        exclusions.append(
            f"{_fmt_int(undated_days)} undated measurement day(s) carried no usable "
            f"source timestamp ({included})"
        )
    exclusions_note = (
        f'<p class="note">Exclusions: {"; ".join(exclusions)}. Raw evidence is '
        "preserved.</p>"
        if exclusions
        else ""
    )
    filter_note = (
        '<p class="note">Filters honored from the canonical read: '
        f"client={esc(client)}, model={esc(model)}, days={esc(days)}, "
        f"granularity={esc(effective_granularity)}. Cost sorting is not "
        "available in canonical mode (estimated cost is a model gap for many "
        "rows).</p>"
    )
    return (
        '<section class="surface">'
        f"{metric_grid}{filter_note}{exclusions_note}"
        + _table(
            "By client",
            "<th>Client</th>",
            by_client_rows,
        )
        + _table(
            "By model",
            "<th>Client</th><th>Model</th>",
            by_model_rows,
        )
        + _table(
            "By period",
            "<th>Period</th>",
            by_period_rows,
        )
        + "</section>"
    )


def _render_canonical_tokens_page(
    read: CanonicalUsageDayRead,
    *,
    client: str,
    model: str,
    days: str,
    effective_granularity: str,
    days_int: int | None,
    today_utc: date,
) -> str:
    return _page_doc(
        page_id="tokens",
        identity_html="",
        notice_html=_canonical_source_note_html(
            surface="usage_days",
            store=read.store,
            projection=read.projection,
            truncated=read.truncated or read.undated_truncated,
            model_gaps=None,
            esc=_esc_html,
        ),
        body_html=_canonical_tokens_body(
            read,
            client=client,
            model=model,
            days=days,
            effective_granularity=effective_granularity,
            days_int=days_int,
            today_utc=today_utc,
            esc=_esc_html,
        ),
    )


def _canonical_task_detail_body(read: CanonicalTaskDetailRead, *, esc: Any) -> str:
    """Honest-subset canonical Task detail (one rm_task_current row)."""

    entry = _canonical_task_entry(read.task, read.sessions, read.sources)
    primary = entry.get("primary_session") or {}
    primary_client = primary.get("client")
    primary_cid = str(primary.get("client_session_id") or "")
    primary_html = (
        f"{esc(primary_client)} · <code>{esc(primary_cid)}</code>"
        if primary_client or primary_cid
        else '<span class="note">unresolved</span>'
    )
    rows = [
        ("State (latest claim)", esc(entry.get("projected_state"))),
        ("Last activity", esc(entry.get("last_activity_at")) or "&mdash;"),
        ("Sessions", _canonical_count_cell(entry.get("session_count"))),
        ("Usage measurements", _canonical_count_cell(entry.get("usage_measurement_count"))),
        ("Input tokens", _canonical_count_cell(entry.get("input_tokens"))),
        ("Output tokens", _canonical_count_cell(entry.get("output_tokens"))),
        ("Total tokens", _canonical_count_cell(entry.get("total_tokens"))),
        ("Primary session", primary_html),
    ]
    rows_html = "".join(
        f"<tr><th scope=\"row\">{label}</th><td>{value}</td></tr>" for label, value in rows
    )
    return (
        '<section class="surface">'
        '<div class="table-wrap"><table><tbody>'
        f"{rows_html}"
        "</tbody></table></div>"
        '<p class="note">State reflects the latest non-superseded work claim; '
        "reported-complete is a claim, not a verification.</p>"
        "</section>"
    )


def _render_canonical_task_detail_page(read: CanonicalTaskDetailRead) -> str:
    return _page_doc(
        page_id="task-detail",
        identity_html="",
        notice_html=_canonical_source_note_html(
            surface="task_detail",
            store=read.store,
            projection=read.projection,
            truncated=False,
            model_gaps=_CANONICAL_TASK_MODEL_GAPS,
            esc=_esc_html,
        ),
        body_html=_canonical_task_detail_body(read, esc=_esc_html),
        page_title=str(read.task.title or "Task"),
        page_subtitle="Canonical task view — the honest subset the canonical model represents.",
        footer_html="agentacct Task Intelligence — localhost only. Missing evidence stays missing; usage is never allocated to make a Task look complete.",
    )


def _canonical_overview_body(
    task_read: CanonicalTaskListRead,
    usage_read: CanonicalUsageDayRead,
    session_read: CanonicalSessionListRead,
    *,
    today_utc: date,
    days_int: int,
    effective_granularity: str,
    esc: Any,
) -> str:
    """Honest-subset canonical Overview: recent work (rm_task_current) + usage
    pulse (rm_usage_day cube) + session roster (base fact table). Each list is
    a bounded newest-first slice; nothing the three models cannot represent is
    synthesized (see _CANONICAL_OVERVIEW_MODEL_GAPS in the source note)."""

    # --- Recent work: newest-first canonical tasks, bounded --------------
    task_entries = [
        _canonical_task_entry(task, task_read.sessions, task_read.sources)
        for task in task_read.tasks
    ]
    shown_tasks = task_entries[:_CANONICAL_OVERVIEW_TASK_ROWS]
    task_rows: list[str] = []
    for entry in shown_tasks:
        primary = entry.get("primary_session") or {}
        p_client = primary.get("client")
        p_cid = str(primary.get("client_session_id") or "")
        p_cid_short = p_cid if len(p_cid) <= 20 else "…" + p_cid[-16:]
        primary_html = (
            f"{esc(p_client)} · <code>{esc(p_cid_short)}</code>"
            if p_client or p_cid
            else '<span class="note">unresolved</span>'
        )
        task_rows.append(
            "<tr>"
            f"<td>{esc(entry.get('title') or 'Untitled task')}</td>"
            f"<td>{esc(entry.get('projected_state'))}</td>"
            f"<td>{esc(entry.get('last_activity_at')) or '&mdash;'}</td>"
            f"<td>{_canonical_count_cell(entry.get('session_count'))}</td>"
            f"<td>{_canonical_count_cell(entry.get('total_tokens'))}</td>"
            f"<td>{primary_html}</td>"
            "</tr>"
        )
    if task_rows:
        task_table = (
            '<div class="table-wrap"><table><thead><tr>'
            "<th>Task</th><th>State (latest claim)</th><th>Last activity</th>"
            "<th>Sessions</th><th>Total tokens</th><th>Primary session</th>"
            "</tr></thead><tbody>" + "".join(task_rows) + "</tbody></table></div>"
        )
    else:
        task_table = '<p class="empty-state">No tasks in the canonical store yet.</p>'
    if len(shown_tasks) < len(task_entries):
        # When the read itself is truncated, len(task_entries) is the query-page
        # limit, NOT the store total — mark it "N+" and name the truncation so
        # the count is never mistaken for the whole store. (A cap of 25 over a
        # 500-row query means the plain "elif truncated" branch is unreachable;
        # truncation must ride in this branch or it is lost from the body.)
        more = "+" if task_read.truncated else ""
        trunc = " · page truncated, the store holds more" if task_read.truncated else ""
        task_cap = (
            f'<p class="note">Showing {_fmt_int(len(shown_tasks))} of '
            f"{_fmt_int(len(task_entries))}{more} tasks on this page (newest "
            f"first){trunc}.</p>"
        )
    else:
        task_cap = ""

    # --- Usage pulse: last-N-day canonical cube totals -------------------
    cube = build_canonical_day_cube(
        usage_read.rows,
        usage_read.undated_rows,
        granularity=effective_granularity,
        days=days_int,
        today=today_utc,
    )
    totals = cube["totals"]
    pulse = (
        '<div class="metric-grid">'
        '<div class="metric"><span class="note">Measurement days</span>'
        f"<strong>{_canonical_count_cell(totals.get('measurement_days'))}</strong></div>"
        '<div class="metric"><span class="note">Fresh tokens</span>'
        f"<strong>{_canonical_count_cell(totals.get('fresh_tokens'))}</strong></div>"
        '<div class="metric"><span class="note">Total (incl. cached)</span>'
        f"<strong>{_canonical_count_cell(totals.get('total_tokens_including_cached'))}</strong></div>"
        f'<div class="metric"><span class="note">Est. cost ({esc(totals.get("cost_completeness"))})</span>'
        f"<strong>{_fmt_usd(totals.get('estimated_cost_usd')) if totals.get('estimated_cost_usd') is not None else '&mdash;'}</strong></div>"
        "</div>"
    )
    pulse_note = (
        f'<p class="note">Usage pulse over the last {_fmt_int(days_int)} UTC '
        f"day(s), {esc(effective_granularity)} granularity. Full explorer: "
        '<a href="/tokens">Usage →</a></p>'
    )
    # Same exclusions disclosure the canonical Tokens body renders: held days
    # are counted in measurement_days but not summed into tokens, and undated
    # rows never enter this bounded window's totals. Without this note the
    # pulse would show non-reconciling numbers (and a "complete" cost label
    # next to an em-dash when held>0) with no explanation — the honesty gap
    # every other canonical usage surface is required to label.
    held = totals.get("held_measurement_days") or 0
    undated = cube["undated"]
    undated_days = undated.get("measurement_days") or 0
    pulse_exclusions: list[str] = []
    if held:
        pulse_exclusions.append(
            f"{_fmt_int(held)} held measurement day(s) are non-additive usage "
            "the canonical model refuses to sum"
        )
    if undated_days:
        included = (
            "included in totals"
            if undated.get("included_in_totals")
            else "excluded from this bounded window"
        )
        pulse_exclusions.append(
            f"{_fmt_int(undated_days)} undated measurement day(s) carried no "
            f"usable source timestamp ({included})"
        )
    pulse_exclusions_note = (
        f'<p class="note">Exclusions: {"; ".join(pulse_exclusions)}. Raw '
        "evidence is preserved.</p>"
        if pulse_exclusions
        else ""
    )

    # --- Session roster: newest-first canonical sessions, bounded --------
    page_sessions = {session.session_id: session for session in session_read.sessions}
    session_entries = [
        _canonical_session_entry(session, session_read, page_sessions)
        for session in session_read.sessions
    ]
    shown_sessions = session_entries[:_CANONICAL_OVERVIEW_SESSION_ROWS]
    session_rows: list[str] = []
    for entry in shown_sessions:
        client = entry.get("client")
        client_html = esc(client) if client else '<span class="note">unresolved</span>'
        cid = str(entry.get("client_session_id") or "")
        cid_short = cid if len(cid) <= 20 else "…" + cid[-16:]
        session_rows.append(
            "<tr>"
            f"<td>{client_html}</td>"
            f'<td><code title="{esc(cid)}">{esc(cid_short)}</code></td>'
            f"<td>{esc(entry.get('session_kind'))}</td>"
            f"<td>{esc(entry.get('last_activity_at')) or '&mdash;'}</td>"
            "</tr>"
        )
    if session_rows:
        roster_table = (
            '<div class="table-wrap"><table><thead><tr>'
            "<th>Client</th><th>Session</th><th>Kind</th><th>Last activity</th>"
            "</tr></thead><tbody>" + "".join(session_rows) + "</tbody></table></div>"
        )
    else:
        roster_table = '<p class="empty-state">No sessions in the canonical store yet.</p>'
    if len(shown_sessions) < len(session_entries):
        # Same as the task caption: a truncated read means the count is the
        # query-page limit, not the store total — mark "N+" and name it.
        more = "+" if session_read.truncated else ""
        trunc = " · page truncated, the store holds more" if session_read.truncated else ""
        roster_cap = (
            f'<p class="note">Showing {_fmt_int(len(shown_sessions))} of '
            f"{_fmt_int(len(session_entries))}{more} sessions on this page "
            f"(newest first){trunc}.</p>"
        )
    else:
        roster_cap = ""

    return (
        '<section class="surface">'
        f'<h2>Recent work <span class="note">({_fmt_int(len(shown_tasks))} shown)</span></h2>'
        f"{task_table}{task_cap}"
        "</section>"
        '<section class="surface">'
        "<h2>Usage pulse</h2>"
        f"{pulse}{pulse_note}{pulse_exclusions_note}"
        "</section>"
        '<section class="surface">'
        f'<h2>Session roster <span class="note">({_fmt_int(len(shown_sessions))} shown)</span></h2>'
        f"{roster_table}{roster_cap}"
        "</section>"
    )


def _canonical_overview_source_note(
    task_read: CanonicalTaskListRead,
    usage_read: CanonicalUsageDayRead,
    session_read: CanonicalSessionListRead,
    *,
    esc: Any,
) -> str:
    """The composite "served from canonical" note for the Overview.

    The shared source note renders the store identity plus ONE projection (the
    task projection); the usage projection staleness and the composition itself
    ride in ``extra_html`` so a reader sees every read model's freshness, and
    truncation is the OR across all three reads.
    """

    usage_projection = usage_read.projection
    usage_state = str(usage_projection.get("state") or "")
    usage_bits = [f"usage projection {esc(usage_state)}"]
    if bool(usage_projection.get("stale")):
        pending = usage_projection.get("pending_writes")
        pending_text = _fmt_int(pending) if isinstance(pending, int) else esc(pending)
        usage_bits.append(f"stale ({pending_text} pending write(s))")
    else:
        usage_bits.append("current")
    extra_html = (
        '<p class="note">Composed from three canonical reads — tasks '
        "(rm_task_current), usage (rm_usage_day), and sessions (base fact "
        f"table, no projection gate). {' · '.join(usage_bits)}.</p>"
    )
    return _canonical_source_note_html(
        surface="overview",
        store=task_read.store,
        projection=task_read.projection,
        truncated=(
            task_read.truncated
            or usage_read.truncated
            or usage_read.undated_truncated
            or session_read.truncated
        ),
        model_gaps=_CANONICAL_OVERVIEW_MODEL_GAPS,
        esc=esc,
        extra_html=extra_html,
    )


def _render_canonical_overview_page(
    task_read: CanonicalTaskListRead,
    usage_read: CanonicalUsageDayRead,
    session_read: CanonicalSessionListRead,
    *,
    subtitle: str,
    today_utc: date,
    days_int: int,
    effective_granularity: str,
) -> str:
    return _page_doc(
        page_id="overview",
        identity_html="",
        notice_html=_canonical_overview_source_note(
            task_read, usage_read, session_read, esc=_esc_html
        ),
        body_html=_canonical_overview_body(
            task_read,
            usage_read,
            session_read,
            today_utc=today_utc,
            days_int=days_int,
            effective_granularity=effective_granularity,
            esc=_esc_html,
        ),
        page_title="Work",
        page_subtitle=subtitle,
    )


def create_local_api_app(
    *,
    store_dir: Path | str,
    usage_discovery: UsageDiscoveryConfig | None = None,
    extra_allowed_hosts: Iterable[str] = (),
) -> FastAPI:
    """Create the local-only agentacct API used by sidecar/MCP surfaces.

    This app exposes report/outcome/event primitives only. It does not call paid
    LLM judges or mutate external agent tools. Bind it to 127.0.0.1 for local use.
    Callers must resolve the store first (no silent home-store default).
    """
    app = FastAPI(title="agentacct local api", version="0.1.0")
    app_pricing_catalog_path = pricing_catalog_path_for_store(store_dir)
    usage_discovery = usage_discovery or UsageDiscoveryConfig()
    # Live refresh progress: one per dashboard process, watched by /refreshing.
    refresh_progress = RefreshProgress()

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
    control_csrf_token = secrets.token_urlsafe(32)
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

    def _collect_run_reports(limit: int = 100) -> list[dict[str, Any]]:
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

    def _mechanical_projection_envelopes() -> tuple[list[Any], dict[str, int]]:
        """Read valid hook projection inputs without mutating the v1 store.

        EvidenceRuntime is intentionally lazy; a normal Work GET must not
        create Evidence v2 storage in a fresh store.  Any projection/read
        failure is additive-only and therefore fail-open.
        """

        evidence_root = Path(store_dir).expanduser() / EVIDENCE_STORE_DIRNAME
        diagnostics: dict[str, int] = {}
        if not service.evidence.enabled or not evidence_root.exists():
            return [], diagnostics
        try:
            # Product reads are bounded. Advanced retains the complete
            # append-only history; Work projects the most recent mechanical
            # window and reports a diagnostic when that window may be
            # truncated. Query every assertion so conflict decisions see the
            # complete idempotency groups present inside the window.
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
        except Exception:  # noqa: BLE001 - additive Evidence v2 cannot break Work.
            diagnostics["read_errors"] = int(diagnostics.get("read_errors") or 0) + 1
            return [], diagnostics

    def _derived_work_ledger() -> dict[str, Any]:
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
        return build_work_ledger(
            service.list_all_events(),
            run_reports=_collect_run_reports(limit=100),
            cost_events=cost_ledger.read_events(),
            session_observations=session_observations,
            session_observation_diagnostics=observation_diagnostics,
            store_project_label=store_project_label,
            store_scope=store_scope,
        )

    def _page_data(
        local_usage_preview: list[ClientUsageEvent] | None = None,
        *,
        continuation_snapshot: Mapping[str, Any] | None = None,
    ) -> _DashboardPageData:
        """Saved-rows page context. Product pages pass NO preview — the live
        client-log scan never runs on their path (PRD §8); only /raw and the
        Refresh POST scan."""

        events = service.list_all_events()
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
        # Budget decisions are capped inside the renderer (with a truncation
        # note); the ledger build sees the full list, matching the JSON path.
        cost_events = sorted(cost_ledger.read_events(), key=lambda item: float(item.get("created_at") or 0.0), reverse=True)
        return _dashboard_page_data(
            events=events,
            cost_events=cost_events,
            run_reports=_collect_run_reports(limit=20),
            session_observations=session_observations,
            session_observation_diagnostics=observation_diagnostics,
            mechanical_check_events=build_mechanical_check_events(mechanical_envelopes),
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
            ingestion_health=ingestion_health.snapshot(),
            task_identity=task_identity,
        )

    def _legacy_recording_configuration_exists(data: _DashboardPageData) -> bool:
        """Conservative compatibility check for installs predating activation state."""

        if data.work_items:
            return True
        instrumentation = (
            data.rollup_summary.get("instrumentation")
            if isinstance(data.rollup_summary.get("instrumentation"), Mapping)
            else {}
        )
        if instrumentation.get("markers_by_client"):
            return True
        if store_scope != "project":
            return False
        project_root = Path(store_dir).expanduser().resolve().parent.parent
        for candidate in (
            project_root / ".mcp.json",
            project_root / ".codex" / "config.toml",
            project_root / ".claude" / "settings.local.json",
        ):
            try:
                if candidate.is_file() and candidate.stat().st_size <= 1_000_000:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    if "agent-chronicle" in text or "agent-sentinel" in text:
                        return True
            except OSError:
                continue
        return False

    def _task_began_after(task: Mapping[str, Any], boundary: float) -> bool:
        sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
        for session in sessions:
            if not isinstance(session, Mapping):
                continue
            for key in ("first_activity_at", "started_at", "created_at"):
                try:
                    observed = float(session.get(key) or 0.0)
                except (TypeError, ValueError, OverflowError):
                    observed = 0.0
                if observed and observed >= boundary:
                    return True
                if observed:
                    break
        return False

    def _activation_payload(data: _DashboardPageData) -> dict[str, Any]:
        projection = _dashboard_task_projection(data)
        install = activation_store.snapshot()
        configured_at = (
            float(install.get("configured_at") or 0.0)
            if isinstance(install, Mapping) and not install.get("issue")
            else 0.0
        )
        if configured_at:
            tasks = projection.get("tasks") if isinstance(projection.get("tasks"), list) else []
            post_configuration_tasks = [
                task
                for task in tasks
                if isinstance(task, Mapping) and _task_began_after(task, configured_at)
            ]
            activation_projection = dict(projection)
            activation_projection["tasks"] = post_configuration_tasks
            summary = dict(
                projection.get("summary") if isinstance(projection.get("summary"), Mapping) else {}
            )
            summary["task_count"] = len(post_configuration_tasks)
            activation_projection["summary"] = summary
        else:
            activation_projection = projection

        source_clients = {
            str(session.get("client"))
            for session in data.rollup_sessions
            if isinstance(session, Mapping) and session.get("client")
        }
        for row in (
            data.ingestion_health.get("sources")
            if isinstance(data.ingestion_health.get("sources"), list)
            else []
        ):
            if not isinstance(row, Mapping):
                continue
            if row.get("last_success_at") is not None or int(row.get("discovered") or 0) > 0:
                source = str(row.get("source") or "")
                if source:
                    source_clients.add(source)
        source_rows = [{"client": client, "status": "found"} for client in sorted(source_clients)]
        watcher = (
            data.ingestion_health.get("watcher")
            if isinstance(data.ingestion_health.get("watcher"), Mapping)
            else {}
        )
        watcher_state = str(watcher.get("state") or "not_configured")
        runtime_status = {
            "state": "running" if watcher_state == "running" else "starting" if watcher_state == "starting" else "stopped",
            "dashboard_health": "healthy",
            "watcher": watcher_state,
        }
        recording_configured = bool(
            isinstance(install, Mapping) and not install.get("issue")
        ) or _legacy_recording_configuration_exists(data)
        payload = build_activation_snapshot(
            source_rows=source_rows,
            ingestion_health=data.ingestion_health,
            task_projection=activation_projection,
            runtime_status=runtime_status,
            recording_configured=recording_configured,
            new_session_required=bool(configured_at and not activation_projection.get("tasks")),
        )
        if isinstance(install, Mapping) and install.get("issue"):
            payload["issues"] = [str(install["issue"])]
        payload["configuration_boundary"] = configured_at or None
        return payload

    def _redirect_with_params(path: str, params: dict[str, Any]) -> RedirectResponse:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        # The CSP rides EVERY dashboard response, redirect shims included
        # ("CSP + localhost guard on every route" is a product rule, not a
        # per-page nicety).
        return RedirectResponse(
            url=path + (f"?{query}" if query else ""),
            status_code=302,
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    def _evidence_product(limit: int = 10_000) -> dict[str, Any]:
        # Indexed Evidence v2 only: unlike the v1 product pages this never
        # loads events.jsonl or rebuilds the historical work ledger.
        return service.evidence.product(limit=limit)

    def _evidence_identity_html(product: dict[str, Any]) -> str:
        summary = product.get("summary") if isinstance(product.get("summary"), dict) else {}
        status = product.get("status") if isinstance(product.get("status"), dict) else {}
        enabled = status.get("enabled") is True
        evidence_count = int(summary.get("evidence_count") or 0)
        preview_scope = enabled and status.get("scope") == "advanced_html_preview"
        preview_limit = int(status.get("limit") or evidence_count)
        evidence_label = "Preview evidence" if preview_scope else "Indexed evidence"
        sources_label = "Preview sources" if preview_scope else "Sources"
        evidence_value = f"Latest {evidence_count:,}" if preview_scope else str(evidence_count)
        evidence_note = (
            f'<div class="note">bounded HTML preview · cap {html.escape(f"{preview_limit:,}")} records</div>'
            if preview_scope
            else ""
        )
        return f"""
        <div class="identity-strip">
          <div class="identity-cell"><div class="label">Evidence v2</div><div class="value">{'Enabled' if enabled else 'Disabled'}</div></div>
          <div class="identity-cell"><div class="label">{evidence_label}</div><div class="value">{html.escape(evidence_value)}</div>{evidence_note}</div>
          <div class="identity-cell"><div class="label">{sources_label}</div><div class="value">{html.escape(str(summary.get('source_count', 0)))}</div></div>
          <div class="identity-cell"><div class="label">Store</div><div class="value">evidence-v2</div></div>
        </div>
        """

    def _render_evidence_page(
        *,
        page_id: str,
        renderer: Any,
    ) -> HTMLResponse:
        product = _evidence_product()
        return HTMLResponse(
            _page_doc(
                page_id=page_id,
                identity_html=_evidence_identity_html(product),
                body_html=renderer(product),
                footer_html="agentacct Evidence v2 &mdash; localhost only. Every cost and outcome keeps its source, assertion, authority, and measurement basis; only explicitly provider-billed evidence is invoice-backed.",
            ),
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    @app.get("/control", response_class=HTMLResponse)
    def control_page(
        notice: str = Query(default="", max_length=240),
        tone: str = Query(default="info", pattern=r"^(?:info|success|error)$"),
    ) -> HTMLResponse:
        page_data = _page_data()
        if not notice and app.state.control_supervisor_error:
            notice = "Owned execution recovery is unavailable. No unverified process was adopted or signalled."
            tone = "error"
        return HTMLResponse(
            _page_doc(
                page_id="control",
                identity_html=page_data.identity_html,
                body_html=render_control_body(
                    _control_payload(),
                    csrf_token=control_csrf_token,
                    notice=notice,
                    tone=tone,
                ),
                footer_html="agentacct Control &mdash; localhost only. External agent and orchestrator processes remain observed-only.",
            ),
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    @app.get("/api/control")
    def control_projection_json() -> dict[str, Any]:
        # No CSRF token or execution authority appears in this read model.
        return _control_payload()

    @app.post("/control/tasks")
    async def create_control_task(request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            ControlTaskCreateRequest,
            expected_csrf_token=control_csrf_token,
        )
        assert isinstance(parsed, ControlTaskCreateRequest)
        base_key = parsed.idempotency_key
        try:
            projection = control_store.project()
            agent = projection.agents.get(parsed.agent_id)
            workspace = projection.workspaces.get(parsed.workspace_id)
            if (
                agent is None
                or not agent.enabled
                or agent.adapter != "local_argv"
                or agent.execution_backend != "subprocess"
            ):
                raise RecordNotFound("enabled owned agent adapter does not exist")
            if workspace is None or not workspace.enabled:
                raise RecordNotFound("enabled workspace does not exist")
            budget_ids: tuple[str, ...] = ()
            if parsed.budget_policy_id:
                policy = projection.budget_policies.get(parsed.budget_policy_id)
                if policy is None or not policy.enabled:
                    raise RecordNotFound("enabled budget policy does not exist")
                budget_ids = (parsed.budget_policy_id,)
            if parsed.task_id:
                observed_projection = _dashboard_task_projection(_page_data())
                if task_identity.resolve(observed_projection, parsed.task_id) is None:
                    raise RecordNotFound("observed task does not exist")
                origin = "observed"
                requested_task_id: str | None = parsed.task_id
            else:
                origin = "planned"
                requested_task_id = None
            task = control_store.create_task(
                origin=origin,
                task_id=requested_task_id,
                idempotency_key=_control_child_key(base_key, "task"),
            )
            checks = tuple(
                line.strip()
                for line in parsed.success_checks.splitlines()
                if line.strip()
            )[:100]
            approval_required = parsed.mutation_mode == "workspace_write"
            contract = control_store.create_contract(
                task.task_id,
                objective=parsed.objective.strip(),
                workspace_id=parsed.workspace_id,
                permission_envelope={
                    "mutation_mode": parsed.mutation_mode,
                    "launch_approval_required": approval_required,
                },
                budget_policy_ids=budget_ids,
                success_checks=checks,
                expected_revision=0,
                idempotency_key=_control_child_key(base_key, "contract"),
            )
            attempt = control_store.create_attempt(
                task.task_id,
                agent_id=parsed.agent_id,
                workspace_id=parsed.workspace_id,
                contract_revision=contract.revision,
                initial_control_state="awaiting_approval" if approval_required else "ready",
                idempotency_key=_control_child_key(base_key, "attempt"),
            )
            if approval_required:
                _request_control_launch_approval(
                    attempt,
                    idempotency_key=_control_child_key(base_key, "approval-request"),
                )
                return _control_redirect("Pending attempt created. Approve its one-time workspace launch before running it.")
            return _control_redirect("Pending attempt created. Review the contract, then press Launch.")
        except (ControlPlaneError, SupervisorError, OSError) as exc:
            return _control_error_redirect(exc)

    @app.post("/control/attempts/{attempt_id}/launch")
    async def launch_control_attempt(attempt_id: str, request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            ControlAttemptActionRequest,
            expected_csrf_token=control_csrf_token,
        )
        assert isinstance(parsed, ControlAttemptActionRequest)
        launch_key = _control_child_key(parsed.idempotency_key, "launch")
        try:
            projection = control_store.project()
            attempt = projection.attempts.get(attempt_id)
            if attempt is None:
                raise RecordNotFound("attempt does not exist")
            _require_control_revision(
                attempt,
                expected_revision=parsed.expected_revision,
                projection=projection,
                retry_key=launch_key,
            )
            if attempt.control_state != "ready":
                raise InvalidTransition("attempt is held by control policy")
            control_supervisor.launch_attempt(attempt_id, idempotency_key=launch_key)
            app.state.control_supervisor_error = None
            return _control_redirect("agentacct proved ownership and launched the pending attempt.")
        except (ControlPlaneError, SupervisorError, OSError) as exc:
            return _control_error_redirect(exc)

    @app.post("/control/attempts/{attempt_id}/cancel")
    async def cancel_control_attempt(attempt_id: str, request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            ControlAttemptActionRequest,
            expected_csrf_token=control_csrf_token,
        )
        assert isinstance(parsed, ControlAttemptActionRequest)
        cancel_key = _control_child_key(parsed.idempotency_key, "cancel")
        try:
            projection = control_store.project()
            attempt = projection.attempts.get(attempt_id)
            if attempt is None:
                raise RecordNotFound("attempt does not exist")
            _require_control_revision(
                attempt,
                expected_revision=parsed.expected_revision,
                projection=projection,
                retry_key=cancel_key,
            )
            control_supervisor.cancel_attempt(attempt_id, idempotency_key=cancel_key)
            return _control_redirect("agentacct verified and stopped its owned process group.")
        except (ControlPlaneError, SupervisorError, OSError) as exc:
            return _control_error_redirect(exc)

    @app.post("/control/attempts/{attempt_id}/retry")
    async def retry_control_attempt(attempt_id: str, request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            ControlAttemptActionRequest,
            expected_csrf_token=control_csrf_token,
        )
        assert isinstance(parsed, ControlAttemptActionRequest)
        base_key = parsed.idempotency_key
        try:
            projection = control_store.project()
            previous = projection.attempts.get(attempt_id)
            if previous is None:
                raise RecordNotFound("attempt does not exist")
            if previous.revision != parsed.expected_revision:
                retry_event = projection.idempotency.get(_control_child_key(base_key, "retry-attempt"))
                if retry_event is None:
                    raise RevisionConflict("attempt changed before retry")
            if previous.execution_state not in {"succeeded", "failed", "cancelled", "lost"}:
                raise InvalidTransition("only a terminal attempt can be retried")
            contract = projection.contracts.get(previous.task_id)
            if contract is None:
                raise RecordNotFound("attempt contract does not exist")
            attempt = control_store.create_attempt(
                previous.task_id,
                agent_id=previous.agent_id,
                workspace_id=previous.workspace_id,
                contract_revision=contract.revision,
                initial_control_state=(
                    "awaiting_approval"
                    if contract_requires_launch_approval(contract.permission_envelope)
                    else "ready"
                ),
                idempotency_key=_control_child_key(base_key, "retry-attempt"),
            )
            if contract_requires_launch_approval(contract.permission_envelope):
                _request_control_launch_approval(
                    attempt,
                    idempotency_key=_control_child_key(base_key, "retry-approval"),
                )
                return _control_redirect("A fresh retry is pending one-time launch approval.")
            control_supervisor.launch_attempt(
                attempt.attempt_id,
                idempotency_key=_control_child_key(base_key, "retry-launch"),
            )
            return _control_redirect("A fresh agentacct-owned retry was launched.")
        except (ControlPlaneError, SupervisorError, OSError) as exc:
            return _control_error_redirect(exc)

    @app.post("/control/attempts/{attempt_id}/request-approval")
    async def request_control_attempt_approval(attempt_id: str, request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            ControlAttemptActionRequest,
            expected_csrf_token=control_csrf_token,
        )
        assert isinstance(parsed, ControlAttemptActionRequest)
        approval_key = _control_child_key(parsed.idempotency_key, "approval-recovery-request")
        try:
            projection = control_store.project()
            attempt = projection.attempts.get(attempt_id)
            if attempt is None:
                raise RecordNotFound("attempt does not exist")
            _require_control_revision(
                attempt,
                expected_revision=parsed.expected_revision,
                projection=projection,
                retry_key=approval_key,
            )
            contract = projection.contracts.get(attempt.task_id)
            if contract is None or contract.revision != attempt.contract_revision:
                raise RecordNotFound("attempt contract does not exist")
            if not contract_requires_launch_approval(contract.permission_envelope):
                raise InvalidTransition("attempt contract does not require launch approval")
            if attempt.execution_state != "pending" or attempt.control_state != "awaiting_approval":
                raise InvalidTransition("attempt is not pending launch approval")
            linked = [approval for approval in projection.approvals.values() if approval.attempt_id == attempt_id]
            if linked and approval_key not in projection.idempotency:
                raise InvalidTransition("attempt already has a launch approval record")
            _request_control_launch_approval(attempt, idempotency_key=approval_key)
            return _control_redirect("A new one-time launch approval request is ready for review.")
        except (ControlPlaneError, SupervisorError, OSError) as exc:
            return _control_error_redirect(exc)

    @app.post("/control/approvals/{approval_id}/decision")
    async def decide_control_approval(approval_id: str, request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            ControlApprovalDecisionRequest,
            expected_csrf_token=control_csrf_token,
        )
        assert isinstance(parsed, ControlApprovalDecisionRequest)
        base_key = parsed.idempotency_key
        decision_key = _control_child_key(base_key, "approval-decision")
        try:
            projection = control_store.project()
            approval = projection.approvals.get(approval_id)
            if approval is None:
                raise RecordNotFound("approval does not exist")
            _require_control_revision(
                approval,
                expected_revision=parsed.expected_revision,
                projection=projection,
                retry_key=decision_key,
            )
            decided, resolved_attempt = control_store.resolve_approval_for_attempt(
                approval_id,
                approve=parsed.decision == "approve",
                decided_by="dashboard-user",
                expected_revision=parsed.expected_revision,
                idempotency_key=decision_key,
            )
            if parsed.decision == "approve":
                assert decided.state == "consumed" and resolved_attempt.control_state == "ready"
                return _control_redirect("Approval consumed once. The pending attempt is now ready to launch.")
            assert decided.state == "rejected" and resolved_attempt.control_state == "policy_hold"
            return _control_redirect("Approval rejected. The attempt remains held.")
        except (ControlPlaneError, SupervisorError, OSError) as exc:
            return _control_error_redirect(exc)

    @app.get("/advanced", response_class=HTMLResponse)
    def advanced_page() -> HTMLResponse:
        product = service.evidence.dashboard_product(limit=ADVANCED_EVIDENCE_PRODUCT_LIMIT)
        current_ingestion_health = ingestion_health.snapshot()
        return HTMLResponse(
            _page_doc(
                page_id="advanced",
                identity_html=_evidence_identity_html(product),
                body_html=(
                    _ingestion_health_panel_html(current_ingestion_health, _esc_html)
                    + render_advanced_index_body(product)
                ),
                footer_html="agentacct Advanced &mdash; localhost only. Product summaries remain separate from forensic identifiers and normalized record details.",
            ),
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    @app.get("/work-graph", response_class=HTMLResponse)
    def work_graph_page() -> HTMLResponse:
        return _render_evidence_page(page_id="work-graph", renderer=render_work_graph_body)

    @app.get("/evidence-matrix", response_class=HTMLResponse)
    def evidence_matrix_page() -> HTMLResponse:
        return _render_evidence_page(page_id="evidence-matrix", renderer=render_evidence_matrix_body)

    @app.get("/discrepancies", response_class=HTMLResponse)
    def discrepancies_page() -> HTMLResponse:
        return _render_evidence_page(page_id="discrepancies", renderer=render_discrepancies_body)

    @app.get("/cost-outcome-basis", response_class=HTMLResponse)
    def cost_outcome_basis_page() -> HTMLResponse:
        return _render_evidence_page(page_id="cost-outcome-basis", renderer=render_cost_outcome_basis_body)

    def _observed_root_session(client: str, session_id: str) -> tuple[ClientSessionRef, Mapping[str, Any]]:
        requested = (client, session_id)
        for group in _overview_root_session_groups(_page_data().rollup_sessions):
            if requested not in group.get("member_keys", set()):
                continue
            root = group.get("root") if isinstance(group.get("root"), Mapping) else {}
            root_client = str(root.get("client") or "")
            root_session_id = str(root.get("client_session_id") or "")
            if root_client and root_session_id:
                return ClientSessionRef(root_client, root_session_id), root
        raise HTTPException(status_code=404, detail="client session was not observed in this store")

    def _observed_root_session_token(token: str) -> tuple[ClientSessionRef, Mapping[str, Any]]:
        for group in _overview_root_session_groups(_page_data().rollup_sessions):
            root = group.get("root") if isinstance(group.get("root"), Mapping) else {}
            client = str(root.get("client") or "")
            session_id = str(root.get("client_session_id") or "")
            if client and session_id and secrets.compare_digest(
                token,
                _task_session_form_token(task_csrf_token, client, session_id),
            ):
                return ClientSessionRef(client, session_id), root
        raise HTTPException(status_code=404, detail="opaque session token is not active in this store")

    def _task_action_redirect() -> RedirectResponse:
        return RedirectResponse(
            url="/#work-feed",
            status_code=303,
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    def _control_projection_readonly() -> ControlProjection:
        """Read existing control state without creating a store on GET."""

        if not control_store.actions_path.exists():
            return ControlProjection()
        return control_store.project()

    def _control_child_key(parent: str, action: str) -> str:
        digest = hashlib.sha256(f"{parent}\0{action}".encode("utf-8")).hexdigest()
        return f"web:{action}:{digest}"

    def _request_control_launch_approval(attempt: Any, *, idempotency_key: str) -> Any:
        """Create or replay a launch approval without changing its expiry intent."""

        projection = control_store.project()
        previous = projection.idempotency.get(idempotency_key)
        if previous is not None:
            existing = projection.approvals.get(previous.target_id)
            if existing is None:
                raise ControlPlaneError("approval request retry has no durable approval")
            expires_at = existing.expires_at
        else:
            expires_at = time.time() + 3600
        return control_store.request_approval(
            attempt.task_id,
            attempt_id=attempt.attempt_id,
            kind="workspace_mutation",
            requested_action="launch",
            requested_by="dashboard-user",
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )

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

    def _control_for_task(public_task_id: str, projection: ControlProjection) -> dict[str, Any]:
        safe = sanitize_control_projection(projection)
        attempts = [row for row in safe["attempts"] if row.get("task_id") == public_task_id]
        attempt_ids = {str(row.get("attempt_id") or "") for row in attempts}
        approvals = [
            row
            for row in safe["approvals"]
            if row.get("task_id") == public_task_id or row.get("attempt_id") in attempt_ids
        ]
        approval_ids = {str(row.get("approval_id") or "") for row in approvals}
        target_ids = {public_task_id, *attempt_ids, *approval_ids}
        events = [row for row in safe["events"] if str(row.get("target_id") or "") in target_ids]
        return {"attempts": attempts, "approvals": approvals, "events": events}

    def _control_redirect(message: str, *, tone: str = "success") -> RedirectResponse:
        query = urlencode({"notice": message[:240], "tone": tone})
        return RedirectResponse(
            url=f"/control?{query}",
            status_code=303,
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    def _control_error_redirect(exc: Exception) -> RedirectResponse:
        if isinstance(exc, RevisionConflict):
            message = "This record changed. Refresh Control and try the action again."
        elif isinstance(exc, IdempotencyConflict):
            message = "That action key was already used for different inputs. Refresh and try again."
        elif isinstance(exc, RecordNotFound):
            message = "The selected control record is no longer available."
        elif isinstance(exc, InvalidTransition):
            message = "That action is no longer valid for the record's current state."
        elif isinstance(exc, SupervisorAlreadyRunning):
            message = "Another agentacct supervisor currently owns this control store."
        elif isinstance(exc, SupervisorError):
            message = "agentacct could not prove this owned-process action was safe. Check the registered adapter and workspace."
        else:
            message = "agentacct rejected the control request. Refresh and review the registered contract."
        return _control_redirect(message, tone="error")

    def _require_control_revision(
        record: Any,
        *,
        expected_revision: int,
        projection: ControlProjection,
        retry_key: str,
    ) -> None:
        if int(record.revision) == expected_revision:
            return
        previous = projection.idempotency.get(retry_key)
        if previous is not None and previous.target_id in {
            getattr(record, "attempt_id", None),
            getattr(record, "approval_id", None),
        }:
            return
        raise RevisionConflict(
            f"control record revision is {record.revision}, expected {expected_revision}"
        )

    def _task_intelligence_payload(public_task_id: str) -> dict[str, Any]:
        projection = _dashboard_task_projection(_page_data())
        task = task_identity.resolve(projection, public_task_id)
        control_projection = _control_projection_readonly()
        if task is None and public_task_id in control_projection.tasks:
            contract = control_projection.contracts.get(public_task_id)
            task = {
                "task_id": public_task_id,
                "primary_root": {},
                "root_keys": [],
                "sessions": [],
                "work_items": [],
                "task_evidence_events": [],
                "usage": {"rows": 0, "total_tokens": None, "estimated_cost_usd": None},
            }
            title = contract.objective if contract is not None else "Planned Task"
        elif task is not None:
            title = _task_title(task)
        else:
            # Generic response: malformed, stale, and unknown ids are
            # intentionally indistinguishable.
            raise HTTPException(status_code=404, detail="Task not found")
        return build_task_intelligence(
            task,
            public_task_id=public_task_id,
            title=title,
            control=_control_for_task(public_task_id, control_projection),
        )

    def _task_intelligence_json_payload(public_task_id: str) -> dict[str, Any]:
        """Task detail JSON: canonical mode when the read flag is on.

        Canonical public ids are importer-minted and DISJOINT from the v1
        HMAC id namespace, so in canonical mode an unknown id is an honest
        404 (the projection gate has already refused stores whose read model
        was never built) — a v1-era id must not silently answer from the v1
        ledger while the list surface serves canonical ids. Two carve-outs:
        PLANNED control-plane tasks (live state the canonical model cannot
        represent) keep serving from v1 with a labeled fallback, and
        unavailability (not a miss) falls back to v1 with the label. A
        payload-build crash on a canonically RESOLVED task is a 503 (visible
        on /health), never a lying not-found. The HTML detail pages stay v1
        in phase 4.2.
        """

        canonical_fallback: dict[str, Any] | None = None
        if service.canonical_read.enabled:
            try:
                read = service.canonical_read.task_detail_read(public_task_id)
            except CanonicalReadUnavailable as unavailable:
                canonical_fallback = _canonical_fallback_label(unavailable)
            else:
                if read.task is None:
                    control_projection = _control_projection_readonly()
                    if public_task_id in control_projection.tasks and task_identity.resolve(
                        _dashboard_task_projection(_page_data()), public_task_id
                    ) is None:
                        # A PLANNED control-plane task: live product state the
                        # canonical model does not represent (task anchors are
                        # minted from sessions only). v1 keeps serving it,
                        # labeled — this is a model gap, not a v1-era id.
                        payload = _task_intelligence_payload(public_task_id)
                        return {
                            **payload,
                            "canonical_read": {
                                "active": False,
                                "source": "v1_fallback",
                                "reason": "not_represented",
                                "detail": (
                                    "planned control-plane tasks are outside "
                                    "the canonical model"
                                ),
                            },
                        }
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            "Task not found in canonical store (canonical read "
                            "active; v1-era task ids live in a separate namespace)"
                        ),
                    )
                try:
                    return _canonical_task_intelligence_payload(read)
                except HTTPException:
                    raise
                except Exception as exc:  # noqa: BLE001 - the reader must never crash this surface.
                    failure = service.canonical_read.surface_failure(
                        f"{type(exc).__name__}: {exc}", surface="task_detail"
                    )
                    # The task EXISTS in the canonical store and the v1 ledger
                    # cannot serve it (disjoint id namespaces) — a v1 fallback
                    # here would be a lying 404. Fail typed and visible
                    # instead: 503 with the crash recorded on /health.
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "canonical task detail failed to build "
                            f"({failure.detail}); recorded on /health "
                            "canonical_read.errors"
                        ),
                    ) from exc
        payload = _task_intelligence_payload(public_task_id)
        if canonical_fallback is not None:
            payload = {**payload, "canonical_read": canonical_fallback}
        return payload

    @app.get("/api/tasks/view/{public_task_id}")
    def task_intelligence_json(public_task_id: str) -> dict[str, Any]:
        return _task_intelligence_json_payload(public_task_id)

    @app.get("/api/tasks/{public_task_id}")
    def task_intelligence_json_canonical(public_task_id: str) -> dict[str, Any]:
        return _task_intelligence_json_payload(public_task_id)

    @app.get("/api/activation")
    def activation_status() -> dict[str, Any]:
        return _activation_payload(_page_data())

    def _task_detail_html_response(public_task_id: str) -> HTMLResponse:
        """Task detail HTML mirroring the JSON canonical contract exactly.

        Flag off -> v1 page. Flag on: a canonically RESOLVED task -> canonical
        page; an unresolved id that is a PLANNED control-plane task -> v1 page
        with a labeled fallback (a model gap, not a v1-era id); any other
        unresolved id -> 404 (canonical public ids are disjoint from v1 HMAC
        ids); a canonical build crash on a resolved task -> 503 visible on
        /health, never a lying v1 404; unavailability -> v1 page + banner."""

        headers = {"Content-Security-Policy": DASHBOARD_CSP}
        if service.canonical_read.enabled:
            try:
                read = service.canonical_read.task_detail_read(public_task_id)
            except CanonicalReadUnavailable as unavailable:
                return HTMLResponse(
                    _render_task_detail_page(
                        _task_intelligence_payload(public_task_id),
                        fallback_notice=_canonical_fallback_notice_html(
                            unavailable, surface="task_detail", esc=_esc_html
                        ),
                    ),
                    headers=headers,
                )
            if read.task is None:
                control_projection = _control_projection_readonly()
                if public_task_id in control_projection.tasks and task_identity.resolve(
                    _dashboard_task_projection(_page_data()), public_task_id
                ) is None:
                    planned = CanonicalReadUnavailable(
                        "not_represented",
                        "planned control-plane tasks are outside the canonical model",
                    )
                    return HTMLResponse(
                        _render_task_detail_page(
                            _task_intelligence_payload(public_task_id),
                            fallback_notice=_canonical_fallback_notice_html(
                                planned, surface="task_detail", esc=_esc_html
                            ),
                        ),
                        headers=headers,
                    )
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "Task not found in canonical store (canonical read "
                        "active; v1-era task ids live in a separate namespace)"
                    ),
                )
            try:
                return HTMLResponse(
                    _render_canonical_task_detail_page(read), headers=headers
                )
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - the reader must never crash this surface.
                failure = service.canonical_read.surface_failure(
                    f"{type(exc).__name__}: {exc}", surface="task_detail"
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "canonical task detail failed to build "
                        f"({failure.detail}); recorded on /health "
                        "canonical_read.errors"
                    ),
                ) from exc
        return HTMLResponse(
            _render_task_detail_page(_task_intelligence_payload(public_task_id)),
            headers=headers,
        )

    @app.get("/tasks/view/{public_task_id}", response_class=HTMLResponse)
    def task_intelligence_page(public_task_id: str) -> HTMLResponse:
        return _task_detail_html_response(public_task_id)

    @app.get("/tasks/{public_task_id}", response_class=HTMLResponse)
    def task_intelligence_page_canonical(public_task_id: str) -> HTMLResponse:
        return _task_detail_html_response(public_task_id)

    @app.get("/tasks")
    def tasks_projection() -> dict[str, Any]:
        """Task list JSON: canonical mode when the read flag is on (schema
        v2-canonical from rm_task_current, three-gate projection staleness
        labels, model gaps named); every unavailability is the labeled v1
        fallback — never silent wrong data, never a crash."""

        canonical_fallback: dict[str, Any] | None = None
        if service.canonical_read.enabled:
            try:
                read = service.canonical_read.task_list_read()
            except CanonicalReadUnavailable as unavailable:
                canonical_fallback = _canonical_fallback_label(unavailable)
            else:
                try:
                    return _canonical_task_projection_payload(read)
                except Exception as exc:  # noqa: BLE001 - the reader must never crash this surface.
                    failure = service.canonical_read.surface_failure(
                        f"{type(exc).__name__}: {exc}", surface="task_list"
                    )
                    canonical_fallback = _canonical_fallback_label(failure)
        payload = _dashboard_task_projection(_page_data())
        result = {**payload, "csrf_token": task_csrf_token}
        if canonical_fallback is not None:
            result["canonical_read"] = canonical_fallback
        return result

    def _observed_finding_target(
        token: str,
        *,
        page_data: _DashboardPageData | None = None,
    ) -> Mapping[str, Any]:
        """Resolve only from the canonical, scope-quarantined finding index."""

        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise HTTPException(status_code=404, detail="Finding not found")
        projection = _dashboard_task_projection(page_data or _page_data())
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
        matches: dict[str, Mapping[str, Any]] = {}
        for episode in episodes:
            event = (
                episode.get("failure_event")
                if isinstance(episode.get("failure_event"), Mapping)
                else None
            )
            if event is None:
                continue
            candidate_token = str(episode.get("finding_token") or "")
            if candidate_token is None or not secrets.compare_digest(token, candidate_token):
                continue
            target_digest = finding_target_digest(event)
            if target_digest is not None:
                matches[target_digest] = episode
        if len(matches) != 1:
            raise HTTPException(status_code=404, detail="Finding not found")
        return next(iter(matches.values()))

    @app.post("/findings/disposition")
    async def set_finding_disposition(request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            FindingDispositionRequest,
            expected_csrf_token=task_csrf_token,
        )
        assert isinstance(parsed, FindingDispositionRequest)
        idempotency_key = (
            f"dashboard:finding:{parsed.finding_token}:"
            f"{parsed.expected_revision}:{parsed.action}"
        )
        try:
            replay = service.replay_finding_disposition(
                action=parsed.action,
                expected_revision=parsed.expected_revision,
                note=parsed.note,
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                return RedirectResponse(
                    url=f"/#finding-{parsed.finding_token}",
                    status_code=303,
                    headers={"Content-Security-Policy": DASHBOARD_CSP},
                )
            with continuation_store.locked_projection() as continuation_snapshot:
                page_data = _page_data(
                    continuation_snapshot=continuation_snapshot.to_dict(),
                )
                target_episode = _observed_finding_target(
                    parsed.finding_token,
                    page_data=page_data,
                )
                target = (
                    target_episode.get("failure_event")
                    if isinstance(target_episode.get("failure_event"), Mapping)
                    else None
                )
                if target is None:
                    raise FindingDispositionNotFound("finding target is unavailable")
                assignment_context = (
                    target_episode.get("assignment_context")
                    if isinstance(target_episode.get("assignment_context"), Mapping)
                    else {}
                )
                task_scope = (
                    assignment_context.get("task_scope")
                    if isinstance(assignment_context.get("task_scope"), Mapping)
                    else None
                )
                service.record_finding_disposition(
                    target_event=target,
                    action=parsed.action,
                    expected_revision=parsed.expected_revision,
                    note=parsed.note,
                    idempotency_key=idempotency_key,
                    task_scope=task_scope,
                    transport="dashboard",
                )
        except FindingDispositionNotFound as exc:
            raise HTTPException(status_code=404, detail="Finding not found") from exc
        except FindingDispositionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="Finding changed or the requested attention action is no longer valid. Refresh and try again.",
            ) from exc
        return RedirectResponse(
            url=f"/#finding-{parsed.finding_token}",
            status_code=303,
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    @app.post("/tasks/link")
    async def link_task_continuation(request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            TaskLinkRequest,
            expected_csrf_token=task_csrf_token,
        )
        assert isinstance(parsed, TaskLinkRequest)
        if parsed.session_token and parsed.target_session_token:
            previous, previous_row = _observed_root_session_token(parsed.session_token)
            continuation, continuation_row = _observed_root_session_token(parsed.target_session_token)
        else:
            assert parsed.client is not None and parsed.client_session_id is not None
            assert parsed.target_client is not None and parsed.target_client_session_id is not None
            previous, previous_row = _observed_root_session(parsed.client, parsed.client_session_id)
            continuation, continuation_row = _observed_root_session(
                parsed.target_client,
                parsed.target_client_session_id,
            )
        cross_scope = previous.client != continuation.client or (
            bool(previous_row.get("project") and continuation_row.get("project"))
            and str(previous_row.get("project")) != str(continuation_row.get("project"))
        )
        if cross_scope and not parsed.confirm_cross_scope:
            raise HTTPException(
                status_code=409,
                detail="cross-client or cross-project continuation links require explicit confirmation",
            )
        try:
            continuation_store.link_sessions(
                previous,
                continuation,
                confirmed_by="dashboard-user",
            )
        except ContinuationTaskError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_action_redirect()

    @app.post("/tasks/unlink")
    async def unlink_task_continuation(request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            TaskUnlinkRequest,
            expected_csrf_token=task_csrf_token,
        )
        assert isinstance(parsed, TaskUnlinkRequest)
        if parsed.session_token:
            session, _root_row = _observed_root_session_token(parsed.session_token)
        else:
            assert parsed.client is not None and parsed.client_session_id is not None
            session, _root_row = _observed_root_session(parsed.client, parsed.client_session_id)
        continuation_projection = continuation_store.project()
        task = continuation_projection.task_for(session)
        if task is None:
            raise HTTPException(status_code=404, detail="session is not part of an explicit continuation Task")
        try:
            continuation_store.unlink_session(
                task.task_id,
                session,
                confirmed_by="dashboard-user",
            )
        except ContinuationTaskError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_action_redirect()

    @app.post("/tasks/rename")
    async def rename_task(request: Request) -> Response:
        parsed = await _read_action_request(
            request,
            TaskRenameRequest,
            expected_csrf_token=task_csrf_token,
        )
        assert isinstance(parsed, TaskRenameRequest)
        try:
            continuation_store.rename_task(
                parsed.task_id,
                parsed.title or None,
                confirmed_by="dashboard-user",
            )
        except (ContinuationTaskError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_action_redirect()

    def _overview_html_response(
        *, import_status: dict[str, Any] | None, usage_breakdown: str = "agent"
    ) -> HTMLResponse:
        """Overview page. Canonical when the read flag is on and the store
        serves; otherwise the v1 Work home. Unlike the 1:1 surfaces, the
        Overview has no single canonical twin — it composes three canonical
        reads (tasks, usage days, sessions). All-or-nothing: any read that
        cannot honestly serve, or a crash composing them, renders the full v1
        page under the VISIBLE labeled fallback banner naming the sub-surface
        that failed (locked principle: any v1 fallback is labeled). The reads
        are attempted in order and short-circuit, so a served count is only
        recorded for reads actually reached."""

        headers = {"Content-Security-Policy": DASHBOARD_CSP}
        page_data = _page_data()
        activation = _activation_payload(page_data)
        if not service.canonical_read.enabled:
            return HTMLResponse(
                _render_overview_page(
                    page_data,
                    import_status=import_status,
                    activation=activation,
                    usage_breakdown=usage_breakdown,
                ),
                headers=headers,
            )
        # 30-day UTC usage-pulse window, mirroring the /tokens default range.
        safe_days = "30"
        effective_granularity = resolve_granularity(safe_days, "auto")
        days_int = days_choice_to_int(safe_days)
        today_utc = datetime.now(timezone.utc).date()
        start_day = (today_utc - timedelta(days=days_int - 1)).isoformat()
        end_day = today_utc.isoformat()
        reads: dict[str, Any] = {}
        for surface, reader in (
            ("task_list", lambda: service.canonical_read.task_list_read()),
            (
                "usage_days",
                lambda: service.canonical_read.usage_day_read(
                    start_day=start_day, end_day=end_day
                ),
            ),
            ("session_list", lambda: service.canonical_read.session_list_read()),
        ):
            try:
                reads[surface] = reader()
            except CanonicalReadUnavailable as unavailable:
                fallback = _canonical_fallback_notice_html(
                    unavailable, surface=surface, esc=_esc_html
                )
                return HTMLResponse(
                    _render_overview_page(
                        page_data,
                        import_status=import_status,
                        activation=activation,
                        fallback_notice=fallback,
                    ),
                    headers=headers,
                )
        # The three reads are independent snapshots (each its own store acquire
        # + transaction), not one atomic read. A promotion swapping the live
        # store file between them would let the source note stamp one store's
        # identity onto another store's rows. Refuse that: if the reads did not
        # all observe one store identity, fall back to the labeled v1 page
        # rather than present a coherent-looking but cross-generation view.
        store_uuids = {
            (reads["task_list"].store or {}).get("store_uuid"),
            (reads["usage_days"].store or {}).get("store_uuid"),
            (reads["session_list"].store or {}).get("store_uuid"),
        }
        if len(store_uuids) > 1:
            failure = service.canonical_read.surface_failure(
                "composite reads observed diverging store identities "
                f"{sorted(str(uuid) for uuid in store_uuids)} — a store swap "
                "landed mid-request; refusing to stamp one store's identity on "
                "another store's rows",
                surface="overview",
            )
            fallback = _canonical_fallback_notice_html(
                failure, surface="overview", esc=_esc_html
            )
            return HTMLResponse(
                _render_overview_page(
                    page_data,
                    import_status=import_status,
                    activation=activation,
                    fallback_notice=fallback,
                    usage_breakdown=usage_breakdown,
                ),
                headers=headers,
            )
        try:
            html = _render_canonical_overview_page(
                reads["task_list"],
                reads["usage_days"],
                reads["session_list"],
                subtitle=_overview_subtitle(page_data),
                today_utc=today_utc,
                days_int=days_int,
                effective_granularity=effective_granularity,
            )
        except Exception as exc:  # noqa: BLE001 - the reader must never crash this surface.
            failure = service.canonical_read.surface_failure(
                f"{type(exc).__name__}: {exc}", surface="overview"
            )
            fallback = _canonical_fallback_notice_html(
                failure, surface="overview", esc=_esc_html
            )
            return HTMLResponse(
                _render_overview_page(
                    page_data,
                    import_status=import_status,
                    activation=activation,
                    fallback_notice=fallback,
                    usage_breakdown=usage_breakdown,
                ),
                headers=headers,
            )
        return HTMLResponse(html, headers=headers)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        tab: str | None = None,
        tools_sort: str | None = None,
        cost_sort: str | None = None,
        activity_sort: str | None = None,
        timeline_view: str | None = None,
        usage_breakdown: str = "agent",
        imported: int | None = Query(default=None, ge=0),
        refreshed: int | None = Query(default=None, ge=0),
        scanned: int | None = Query(default=None, ge=0),
        priced: int | None = Query(default=None, ge=0),
        migrated: int | None = Query(default=None, ge=0),
        source_namespace_conflicts: int | None = Query(default=None, ge=0),
        source_namespace_adoptions: int | None = Query(default=None, ge=0),
        concurrent_refresh_conflicts: int | None = Query(default=None, ge=0),
        incomplete_alias_migrations: int | None = Query(default=None, ge=0),
        observed_sessions: int | None = Query(default=None, ge=0),
        usage_sessions: int | None = Query(default=None, ge=0),
        sessions_without_usage: int | None = Query(default=None, ge=0),
        imported_session_observations: int | None = Query(default=None, ge=0),
        preserved_session_observations: int | None = Query(default=None, ge=0),
        session_observation_conflicts: int | None = Query(default=None, ge=0),
        evidence_reconcile_enabled: bool | None = None,
        evidence_reconcile_errors: int | None = Query(default=None, ge=0),
        evidence_reconcile_conflicts: int | None = Query(default=None, ge=0),
        evidence_reconcile_complete: bool | None = None,
    ) -> Response:
        """Overview page. Back-compat for the tab era: ``/?tab=raw`` 302s to
        /raw and every other ``tab`` value (the old ``_safe_choice`` fallback
        rendered the product tab) 302s to the bare Overview; whitelisted
        params travel with the redirect."""

        if tab is not None:
            forward = {
                "tools_sort": tools_sort,
                "cost_sort": cost_sort,
                "activity_sort": activity_sort,
                "timeline_view": timeline_view,
                # Only a valid non-default breakdown rides the redirect, so the
                # legacy tab shims keep their exact bare-path Location.
                "usage_breakdown": usage_breakdown if usage_breakdown in {"model", "agent-model"} else None,
                "imported": imported,
                "refreshed": refreshed,
                "scanned": scanned,
                "priced": priced,
                "migrated": migrated,
                "source_namespace_conflicts": source_namespace_conflicts,
                "source_namespace_adoptions": source_namespace_adoptions,
                "concurrent_refresh_conflicts": concurrent_refresh_conflicts,
                "incomplete_alias_migrations": incomplete_alias_migrations,
                "observed_sessions": observed_sessions,
                "usage_sessions": usage_sessions,
                "sessions_without_usage": sessions_without_usage,
                "imported_session_observations": imported_session_observations,
                "preserved_session_observations": preserved_session_observations,
                "session_observation_conflicts": session_observation_conflicts,
                "evidence_reconcile_enabled": evidence_reconcile_enabled,
                "evidence_reconcile_errors": evidence_reconcile_errors,
                "evidence_reconcile_conflicts": evidence_reconcile_conflicts,
                "evidence_reconcile_complete": evidence_reconcile_complete,
            }
            return _redirect_with_params("/raw" if tab == "raw" else "/", forward)
        import_status = None
        if (
            imported is not None
            or refreshed is not None
            or scanned is not None
            or priced is not None
            or source_namespace_conflicts is not None
            or source_namespace_adoptions is not None
            or concurrent_refresh_conflicts is not None
            or incomplete_alias_migrations is not None
            or observed_sessions is not None
            or usage_sessions is not None
            or sessions_without_usage is not None
            or imported_session_observations is not None
            or preserved_session_observations is not None
            or session_observation_conflicts is not None
            or evidence_reconcile_enabled is not None
            or evidence_reconcile_errors is not None
            or evidence_reconcile_conflicts is not None
            or evidence_reconcile_complete is not None
        ):
            import_status = {
                "imported": imported or 0,
                "refreshed": refreshed if refreshed is not None else imported or 0,
                "scanned": scanned or 0,
                "priced": priced or 0,
                "migrated": migrated or 0,
                "source_namespace_conflicts": source_namespace_conflicts or 0,
                "source_namespace_adoptions": source_namespace_adoptions or 0,
                "concurrent_refresh_conflicts": concurrent_refresh_conflicts or 0,
                "incomplete_alias_migrations": incomplete_alias_migrations or 0,
                "observed_sessions": observed_sessions or 0,
                "usage_sessions": usage_sessions or 0,
                "sessions_without_usage": sessions_without_usage or 0,
                "imported_session_observations": imported_session_observations or 0,
                "preserved_session_observations": preserved_session_observations or 0,
                "session_observation_conflicts": session_observation_conflicts or 0,
                "evidence_reconcile_enabled": evidence_reconcile_enabled,
                "evidence_reconcile_errors": evidence_reconcile_errors or 0,
                "evidence_reconcile_conflicts": evidence_reconcile_conflicts or 0,
                "evidence_reconcile_complete": evidence_reconcile_complete,
            }
        # No legacy query-string summary: read the one-time refresh flash cookie
        # instead, so the post-refresh banner shows once from a clean ``/`` URL.
        flash_consumed = False
        if import_status is None:
            flash = _decode_refresh_flash(request.cookies.get(REFRESH_FLASH_COOKIE))
            if flash is not None:
                import_status = flash
                flash_consumed = True
        response = _overview_html_response(
            import_status=import_status, usage_breakdown=usage_breakdown
        )
        if flash_consumed:
            # Show it once: clear the cookie so a reload does not re-flash.
            response.delete_cookie(REFRESH_FLASH_COOKIE, path="/")
        return response

    def _tokens_html_response(
        *, client: str, model: str, days: str, granularity: str, cost_sort: str
    ) -> HTMLResponse:
        """Tokens/usage HTML: canonical when the read flag is on and the store
        serves; otherwise the v1 explorer. A flag-on canonical read that cannot
        honestly serve renders v1 under the VISIBLE labeled fallback banner.

        The range/filter mapping mirrors the JSON /usage/summary canonical path
        (UTC-day window, whitelisted params normalized the tolerant HTML way);
        an unknown ``model`` rides through to the empty canonical result."""

        headers = {"Content-Security-Policy": DASHBOARD_CSP}
        if service.canonical_read.enabled:
            safe_client = _safe_choice(client, {"all", *KNOWN_USAGE_CLIENTS}, "all")
            safe_days = _safe_choice(days, set(USAGE_CUBE_DAYS_CHOICES), "30")
            safe_granularity = _safe_choice(
                granularity, {"auto", *USAGE_CUBE_GRANULARITY_CHOICES}, "auto"
            )
            days_int = days_choice_to_int(safe_days)
            effective_granularity = resolve_granularity(safe_days, safe_granularity)
            today_utc = datetime.now(timezone.utc).date()
            if days_int is None:
                start_day, end_day = FIRST_DATED_DAY, LAST_DATED_DAY
            else:
                start_day = (today_utc - timedelta(days=days_int - 1)).isoformat()
                end_day = today_utc.isoformat()
            try:
                read = service.canonical_read.usage_day_read(
                    start_day=start_day,
                    end_day=end_day,
                    client=None if safe_client == "all" else safe_client,
                    model=None if model == "all" else model,
                )
            except CanonicalReadUnavailable as unavailable:
                fallback = _canonical_fallback_notice_html(
                    unavailable, surface="usage_days", esc=_esc_html
                )
            else:
                try:
                    return HTMLResponse(
                        _render_canonical_tokens_page(
                            read,
                            client=safe_client,
                            model=model,
                            days=safe_days,
                            effective_granularity=effective_granularity,
                            days_int=days_int,
                            today_utc=today_utc,
                        ),
                        headers=headers,
                    )
                except Exception as exc:  # noqa: BLE001 - the reader must never crash this surface.
                    failure = service.canonical_read.surface_failure(
                        f"{type(exc).__name__}: {exc}", surface="usage_days"
                    )
                    fallback = _canonical_fallback_notice_html(
                        failure, surface="usage_days", esc=_esc_html
                    )
            return HTMLResponse(
                _render_tokens_page(
                    _page_data(),
                    client=client, model=model, days=days, granularity=granularity, cost_sort=cost_sort,
                    fallback_notice=fallback,
                ),
                headers=headers,
            )
        return HTMLResponse(
            _render_tokens_page(
                _page_data(), client=client, model=model, days=days, granularity=granularity, cost_sort=cost_sort
            ),
            headers=headers,
        )

    @app.get("/tokens", response_class=HTMLResponse)
    def tokens_page(
        client: str = "all",
        model: str = "all",
        days: str = "30",
        granularity: str = "auto",
        cost_sort: str = "total",
    ) -> HTMLResponse:
        """Tokens explorer (PRD §5.2). Saved rows only — never a live scan.

        Every filter combination is a URL. Whitelisted params (client, days,
        granularity, cost_sort) fall back to their defaults on unknown values
        (never a 500, matching the page-wide _safe_choice pattern); an
        unknown ``model`` renders the EMPTY result with the filter echoed —
        never a guess (locked decision). JSON parity: GET /usage/summary."""

        return _tokens_html_response(
            client=client, model=model, days=days, granularity=granularity, cost_sort=cost_sort
        )

    @app.get("/raw", response_class=HTMLResponse)
    def raw_dashboard(
        tools_sort: str = "tokens",
        cost_sort: str = "total",
        activity_sort: str = "newest",
        timeline_view: str = "grouped",
    ) -> HTMLResponse:
        """Raw/debug page — the ONE page that still runs the live client-log
        scan (source discovery + unsaved preview sections), preserving the
        old raw tab and the Preview action semantics verbatim."""

        runs = service.list_runs(limit=20)
        usage_sources = _discover_local_usage_sources(usage_discovery)
        local_usage_preview = _discover_local_usage(usage_discovery, limit_sessions=DASHBOARD_USAGE_LIMIT_SESSIONS)
        return HTMLResponse(
            _render_raw_page(
                _page_data(local_usage_preview),
                runs=runs,
                usage_sources=usage_sources,
                tools_sort=tools_sort,
                cost_sort=cost_sort,
                activity_sort=activity_sort,
                timeline_view=timeline_view,
            ),
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    @app.post("/usage/import-local")
    def usage_import_local_from_dashboard(request: Request) -> RedirectResponse:
        # TTL auto-refresh of the LiteLLM pricing snapshot on the dashboard
        # refresh path (the dashboard always prices). env_pinned comes from
        # the middleware's pre-pin stash: the middleware pins the env to the
        # store's OWN snapshot for every request, and refreshing that snapshot
        # is exactly the intent — only a real user pin blocks the fetch.
        # Best-effort by contract: a failed fetch never fails the import.
        refresh_outcome = ensure_fresh_pricing_snapshot(
            store_dir, env_pinned=bool(getattr(request.state, "pricing_catalog_user_pinned", False))
        )
        if refresh_outcome.get("refreshed") and read_env_alias(PRICING_CATALOG_PATH_ENV) is None:
            # First-ever snapshot for this store: it did not exist when the
            # middleware ran, so pin it for the remainder of this request
            # (the middleware's finally restores the previous env after).
            if app_pricing_catalog_path is not None and app_pricing_catalog_path.exists():
                os.environ[PRICING_CATALOG_PATH_ENV] = str(app_pricing_catalog_path)
                reset_pricing_catalog_cache()
        # Resolve the pricing catalog HERE, while this request still has the env
        # correctly pinned, and hand the object to the worker: the background
        # refresh then prices against it via pricing_catalog_scope, immune to
        # another request's middleware pinning/unpinning the env concurrently.
        catalog = pricing_catalog()
        job_id = refresh_progress.begin()
        if job_id is not None:
            def _run_refresh(bound_catalog: Any = catalog) -> None:
                try:
                    with pricing_catalog_scope(bound_catalog), usage_progress_scope(refresh_progress):
                        status = _import_local_usage_events(
                            service, usage_discovery, ingestion_health
                        )
                    refresh_progress.finish(status)
                except Exception as exc:  # noqa: BLE001 - surfaced on the progress page.
                    refresh_progress.fail(f"{type(exc).__name__}: {exc}")

            threading.Thread(
                target=_run_refresh, name="chronicle-refresh", daemon=True
            ).start()
        # Either we started a job, or one was already running — either way, watch
        # it on the no-JS progress page (which 303s home with the flash when done).
        return RedirectResponse(
            url="/refreshing",
            status_code=303,
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    @app.get("/refreshing", response_class=HTMLResponse)
    def refreshing_status_page() -> Response:
        """No-JS live progress for a background refresh. While running it serves
        a 1-second self meta-refresh; when the refresh finishes it 303s to / and
        sets the one-time flash cookie (so the banner shows once, as before);
        with no active refresh it just goes home."""

        snap = refresh_progress.snapshot()
        state = snap.get("state")
        if state == "done":
            result = refresh_progress.consume_result()
            response = RedirectResponse(
                url="/", status_code=303, headers={"Content-Security-Policy": DASHBOARD_CSP}
            )
            if result is not None:
                response.set_cookie(
                    REFRESH_FLASH_COOKIE,
                    _encode_refresh_flash(result),
                    max_age=REFRESH_FLASH_MAX_AGE,
                    path="/",
                    httponly=True,
                    samesite="strict",
                )
            return response
        if state == "idle":
            return RedirectResponse(
                url="/", status_code=303, headers={"Content-Security-Policy": DASHBOARD_CSP}
            )
        # running or error → render the progress page.
        return HTMLResponse(
            _render_refreshing_page(snap),
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )

    @app.post("/usage/import-local/{client}")
    def usage_import_client_from_dashboard(client: str) -> RedirectResponse:
        """Retry one implemented client importer after a source/adapter repair.

        This recovery path deliberately avoids the global refresh's pricing
        catalog update and cannot migrate or refresh unrelated clients.
        """

        if client not in SUPPORTED_CLIENTS:
            raise HTTPException(status_code=404, detail="unsupported local usage client")

        status = _import_local_usage_events(
            service,
            usage_discovery,
            ingestion_health,
            client=client,
        )
        response = RedirectResponse(
            url="/",
            status_code=303,
            headers={"Content-Security-Policy": DASHBOARD_CSP},
        )
        response.set_cookie(
            REFRESH_FLASH_COOKIE,
            _encode_refresh_flash(status),
            max_age=REFRESH_FLASH_MAX_AGE,
            path="/",
            httponly=True,
            samesite="strict",
        )
        return response

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
            # Canonical live shadow (migration phase 3): in-process counters
            # for THIS dashboard process only — MCP servers and the watcher
            # keep their own runtimes. Enough to see a silently failing
            # shadow lane; per-process aggregation is a cutover concern.
            "canonical_live": service.canonical_live.status(),
            # Canonical read path (migration phase 4): same in-process scope.
            # A read flag that silently falls back to v1 forever must be
            # discoverable here, not only in individual response labels.
            "canonical_read": service.canonical_read.status(),
            # Canonical STORE diagnostics (phase 4.3): flag-independent
            # read-only probe — store presence/role/schema plus per-projection
            # built/stale state. This is the operator's cutover-readiness
            # evidence while production reads still serve v1; never raises.
            "canonical_store": service.canonical_read.health_probe(),
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

    def _sessions_html_response(
        *, client: str, project: str, join: str, kind: str, days: str, limit: int,
        sort: str = "recent", work: str = "all", show: int = SESSION_ROLLUP_DISPLAY_LIMIT,
    ) -> HTMLResponse:
        """Sessions HTML: canonical when the read flag is on and the store
        serves; otherwise the v1 explorer. A flag-on canonical read that cannot
        honestly serve renders v1 under the VISIBLE labeled fallback banner
        (never a silent v1 render presented as canonical)."""

        headers = {"Content-Security-Policy": DASHBOARD_CSP}
        if service.canonical_read.enabled:
            try:
                read = service.canonical_read.session_list_read(limit=limit)
            except CanonicalReadUnavailable as unavailable:
                fallback = _canonical_fallback_notice_html(
                    unavailable, surface="session_list", esc=_esc_html
                )
            else:
                try:
                    return HTMLResponse(
                        _render_canonical_sessions_page(
                            read, client=client, project=project, join=join, kind=kind, days=days
                        ),
                        headers=headers,
                    )
                except Exception as exc:  # noqa: BLE001 - the reader must never crash this surface.
                    failure = service.canonical_read.surface_failure(
                        f"{type(exc).__name__}: {exc}", surface="session_list"
                    )
                    fallback = _canonical_fallback_notice_html(
                        failure, surface="session_list", esc=_esc_html
                    )
            return HTMLResponse(
                _render_sessions_page(
                    _page_data(),
                    client=client, project=project, join=join, kind=kind, days=days,
                    sort=sort, work=work, show=show,
                    fallback_notice=fallback,
                ),
                headers=headers,
            )
        return HTMLResponse(
            _render_sessions_page(
                _page_data(), client=client, project=project, join=join, kind=kind, days=days,
                sort=sort, work=work, show=show,
            ),
            headers=headers,
        )

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

        Phase 3.5a: this path is ALSO the Sessions HTML page (PRD §4 IA).
        Dispatch is on the Accept header, parsed with q-values
        (_accepts_html): a client that prefers ``text/html`` over
        ``application/json`` (browsers) gets the rendered Sessions explorer;
        everything else (bare ``*/*`` defaults — curl, httpx, scripts —
        absent headers, explicit JSON, or ``text/html;q=0`` refusals) gets
        the JSON rollup below, byte-for-byte unchanged from the tab era.

        Phase 3.5c: ``client``/``project``/``join``/``kind``/``days`` filter
        the HTML session list and work items only (whitelisted; unknown
        values fall back to defaults, unknown ``project`` renders the empty
        result with the filter echoed — never a guess). The JSON rollup
        IGNORES them, and SAYS so on the wire: when any of the five HTML
        filter params rides a JSON request, the payload gains an additive
        ``ignored_html_params`` list naming them, so a script can never
        mistake the unfiltered rollup for a filtered one. Additive rollup fields (schema
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
        if _accepts_html(request.headers.get("accept")):
            return _sessions_html_response(
                client=client, project=project, join=join, kind=kind, days=days, limit=limit,
                sort=sort, work=work, show=show,
            )
        ignored = sorted(
            name
            for name in ("client", "project", "join", "kind", "days", "sort", "work", "show")
            if name in request.query_params
        )
        # Canonical read path (migration phase 4.2): the sessions table is a
        # base fact table (always current — no projection gate); the HTML
        # filter params are ignored with the same additive wire honesty as
        # the v1 rollup, and any unavailability is the labeled v1 fallback.
        canonical_fallback: dict[str, Any] | None = None
        if service.canonical_read.enabled:
            try:
                canonical_sessions = service.canonical_read.session_list_read(limit=limit)
            except CanonicalReadUnavailable as unavailable:
                canonical_fallback = _canonical_fallback_label(unavailable)
            else:
                try:
                    canonical_payload = _canonical_session_rollup_payload(canonical_sessions)
                    if ignored:
                        canonical_payload["ignored_html_params"] = ignored
                    return canonical_payload
                except Exception as exc:  # noqa: BLE001 - the reader must never crash this surface.
                    failure = service.canonical_read.surface_failure(
                        f"{type(exc).__name__}: {exc}", surface="session_list"
                    )
                    canonical_fallback = _canonical_fallback_label(failure)
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
        if canonical_fallback is not None:
            payload["canonical_read"] = canonical_fallback
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

    async def _bounded_json_body(request: Request, *, maximum_bytes: int = 4 * 1024 * 1024) -> dict[str, Any]:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > maximum_bytes:
                    raise HTTPException(status_code=413, detail="connector JSON payload is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content-length") from exc
        body = await request.body()
        if len(body) > maximum_bytes:
            raise HTTPException(status_code=413, detail="connector JSON payload is too large")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="connector body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="connector body must be a JSON object")
        return payload

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

    def _import_connector(records: Any, *, connector: str) -> dict[str, Any]:
        result = import_connector_records(service.evidence, records, connector=connector)
        payload = result.to_dict()
        if result.error_count:
            # A committed prefix is still reported in the response. Do not hide
            # partial durable progress behind a generic 500.
            payload["partial"] = True
        return payload

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

    @app.post("/v1/traces")
    async def ingest_otlp_traces(request: Request) -> dict[str, Any]:
        """Local OTLP/HTTP JSON receiver for OpenLIT and native agent spans."""

        payload = await _bounded_json_body(request)
        try:
            result = _import_connector(OpenLITOTLPConnector().read(payload), connector="openlit")
        except ConnectorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"partialSuccess": {}, "chronicle": result}

    @app.post("/connectors/openlit/import")
    async def import_openlit(request: Request) -> dict[str, Any]:
        payload = await _bounded_json_body(request)
        try:
            return _import_connector(OpenLITOTLPConnector().read(payload), connector="openlit")
        except ConnectorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/connectors/paperclip/import")
    async def import_paperclip(request: Request) -> dict[str, Any]:
        payload = await _bounded_json_body(request)
        try:
            return _import_connector(PaperclipSnapshotConnector().read(payload), connector="paperclip")
        except ConnectorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/connectors/entire/import")
    def import_entire(request: EntireImportRequest) -> dict[str, Any]:
        try:
            records = EntireGitConnector(request.repository, max_commits=request.max_commits).read()
            return _import_connector(records, connector="entire")
        except (ConnectorError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    def _canonical_usage_summary_payload(
        *,
        read: CanonicalUsageDayRead,
        client: str,
        model: str,
        days: str,
        granularity_requested: str,
        effective_granularity: str,
        days_int: int | None,
        today_utc: date,
    ) -> dict[str, Any]:
        """The canonical-mode /usage/summary body (schema v2-canonical).

        Same surface shape as the v1 cube where semantics genuinely match;
        fields the day model cannot represent are explicit None (never 0),
        and the epoch-sentinel rows ride in ``undated`` / the "unknown"
        period — never as a real 1970 date.
        """

        cube = build_canonical_day_cube(
            read.rows,
            read.undated_rows,
            granularity=effective_granularity,
            days=days_int,
            today=today_utc,
        )
        projection = _canonical_projection_label(read.projection)
        return {
            "schema_version": CANONICAL_USAGE_SUMMARY_SCHEMA_VERSION,
            "canonical_read": {
                "active": True,
                "source": "canonical",
                "day_basis": read.day_basis,
                "store": dict(read.store),
                "projection": projection,
                "truncated": bool(read.truncated),
                "undated_truncated": bool(read.undated_truncated),
            },
            "filters_echo": {
                "client": client,
                "model": model,
                "days": days,
                "granularity": effective_granularity,
                "granularity_requested": granularity_requested,
                "model_matches_saved_rows": (
                    True if model == "all" else bool(read.model_matches_store)
                ),
            },
            "totals": cube["totals"],
            "usage_exclusions": {
                "held_measurement_days": cube["totals"]["held_measurement_days"],
                "undated_measurement_days": cube["undated"]["measurement_days"],
                "reason": (
                    "held rows are non-additive usage the canonical model refuses "
                    "to sum; undated rows carried no usable source timestamp"
                ),
                "raw_evidence_preserved": True,
            },
            "range_context": {
                # Not computed on the canonical path yet (phase 4.1); null
                # means "not computed", never "no history outside range".
                "history_outside_range": None,
                "reason": "not_computed_in_canonical_read_v1",
            },
            "by_client": cube["by_client"],
            "by_model": cube["by_model"],
            "by_period": cube["by_period"],
            "undated": cube["undated"],
        }

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
        # Canonical read path (migration phase 4.1): default OFF. Enabled, it
        # serves this surface from rm_usage_day with stale/pending labels;
        # when the store cannot honestly serve, the v1 response below carries
        # the labeled fallback — never silent wrong data, never a crash.
        canonical_fallback: dict[str, Any] | None = None
        if service.canonical_read.enabled:
            days_int = days_choice_to_int(days)
            effective_granularity = resolve_granularity(days, granularity)
            # rm_usage_day labels days in UTC (labeled in the response);
            # deriving the range from a local today would misalign the window.
            today_utc = datetime.now(timezone.utc).date()
            if days_int is None:
                start_day, end_day = FIRST_DATED_DAY, LAST_DATED_DAY
            else:
                start_day = (today_utc - timedelta(days=days_int - 1)).isoformat()
                end_day = today_utc.isoformat()
            try:
                canonical_read = service.canonical_read.usage_day_read(
                    start_day=start_day,
                    end_day=end_day,
                    client=None if client == "all" else client,
                    model=None if model == "all" else model,
                )
            except CanonicalReadUnavailable as unavailable:
                canonical_fallback = _canonical_fallback_label(unavailable)
            else:
                try:
                    return _canonical_usage_summary_payload(
                        read=canonical_read,
                        client=client,
                        model=model,
                        days=days,
                        granularity_requested=granularity,
                        effective_granularity=effective_granularity,
                        days_int=days_int,
                        today_utc=today_utc,
                    )
                except Exception as exc:  # noqa: BLE001 - the reader must never crash this surface.
                    failure = service.canonical_read.surface_failure(
                        f"{type(exc).__name__}: {exc}", surface="usage_days"
                    )
                    canonical_fallback = _canonical_fallback_label(failure)
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
        if canonical_fallback is not None:
            payload["canonical_read"] = canonical_fallback
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

    @app.post("/runs/{run_id}/judge/prepare")
    def prepare_judge(run_id: str, request: JudgePrepareRequest) -> dict[str, Any]:
        try:
            return service.prepare_judge(run_id, task_goal=request.task_goal, rubric=request.rubric, write_package=request.write_package)
        except FileNotFoundError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise _invalid(exc) from exc

    @app.post("/runs/{run_id}/value/compute")
    def compute_value(run_id: str, request: ValueComputeRequest) -> dict[str, Any]:
        try:
            return {"value": service.compute_value(run_id, budget_usd=request.budget_usd)}
        except FileNotFoundError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise _invalid(exc) from exc

    return app
