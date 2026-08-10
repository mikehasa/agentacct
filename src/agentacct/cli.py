from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import shlex
import shutil
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Callable, Iterator, Mapping, NoReturn, Optional, Sequence

# Shared with the sentinel-claude/sentinel-codex wrapper entry points so all
# three installed console scripts fail fast identically on native Windows. The
# import chain below reaches fcntl (service.py) and POSIX signals (runner.py),
# so this must run before those imports. Aliased to the historical private name
# for the existing unit test.
from .platform_support import exit_if_unsupported_platform as _exit_if_unsupported_platform

_exit_if_unsupported_platform(sys.platform)

import typer
from rich.console import Console
from rich.markup import escape as _rich_escape
from rich.table import Table

from .agent_loop import AgentLoopOptions, run_agent_like_loop
from .agent_smoke import AgentSmokeError, LiveAgent, assert_live_agent_smoke_passed, run_live_agent_smoke
from .activation import ActivationStateError, ActivationStateStore, RuntimeManager, RuntimeManagerError
from . import autostart as autostart_mod
from .autostart import AutostartError
from .agent_capabilities import agent_capability_manifest
from .api import UsageDiscoveryConfig, create_local_api_app
from .usage_snapshot import (
    NOW_WINDOW_ALIASES,
    ORIGIN_LABELS,
    build_usage_snapshot,
    cost_text,
    format_tokens,
    humanize_seconds,
    latest_limit_events,
    limit_json_entry,
    limit_teaser_lines,
    usage_bar,
    window_label,
)
from .client_usage import (
    SUPPORTED_CLIENTS,
    apply_pricing_estimate_to_event,
    bind_discovered_usage_source_namespaces,
    build_usage_import_diagnostics,
    build_usage_import_write_batches,
    classify_usage_write_conflict_candidates,
    complete_session_observation_reconciliation_clients,
    codex_parse_cache_scope,
    describe_scanned_client_homes,
    discover_client_usage_with_diagnostics,
    plan_local_usage_import,
    promote_unknown_cost_reprices,
    recognized_local_usage_row_identity,
    select_usage_import_candidates,
    source_namespace_adoption_candidates,
    usage_less_session_observations,
)
from .ingestion_health import (
    EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE,
    IngestionHealthStore,
    apply_evidence_refreshable_usage_health,
    evidence_refreshable_usage_failed,
    health_scan_results,
    importer_build_id,
    session_observation_conflict_error_code,
)
from .capture import CaptureContext, DEFAULT_CAPTURE_REGISTRY, render_hook_manifest
from .capture.registry import DEFAULT_MAX_PAYLOAD_BYTES
from .capture_runtime import capture_hook_payload
from .connector_runtime import import_connector_records
from .connectors import (
    ConnectorError,
    ControlSignal,
    EntireGitConnector,
    OpenLITOTLPConnector,
    PaperclipSnapshotConnector,
    evaluate_control_signal,
)
from .connectors.control import validate_supporting_evidence
from .control_plane import (
    ControlPlaneError,
    ControlProjection,
    ControlStore,
    RevisionConflict,
    contract_requires_launch_approval,
)
from .cost import (
    PRICING_CATALOG_PATH_ENV,
    CostLedger,
    CostPolicy,
    SubscriptionPlan,
    SubscriptionStore,
    activate_pricing_catalog_for_store,
    model_pricing_entry,
    pricing_catalog_path_for_store,
    pricing_catalog,
    reset_pricing_catalog_cache,
    estimate_subscription_cost,
)
from .context_bridge import build_client_context_join_health
from .hooks import (
    CLAUDE_HOOK_RELATIVE_PATH,
    capture_claude_code_client_context,
    capture_tool_activity,
    capture_mechanical_check,
    claude_code_hook_context_status,
    claude_code_hook_doctor_checks,
    claude_code_hook_paths,
    claude_session_start_response,
    evaluate_stdin_json,
    install_claude_code_hook,
)
from .log_evidence import build_log_evidence_index, summarize_log_evidence_donor_rows
from . import install_guide
from .install_guide import full_prompt as install_guide_full_prompt, one_line_prompt as install_guide_one_line_prompt
from .mcp import DEGRADED_NO_STORE_MESSAGE, TOOLS, SentinelMCPServer, run_mcp_event_workflow_smoke, serve_stdio
from .mcp_client_smoke import MCPClientSmokeError, assert_mcp_client_smoke_passed, run_mcp_client_smoke
from .outcome import (
    apply_judge_result,
    apply_value_score,
    build_judge_package,
    build_machine_check_outcome,
    compute_advisory_value_score,
    read_outcome,
    run_openrouter_judge,
    write_outcome,
)
from .policy import DEFAULT_POLICY_FILE, default_policy_path, load_and_validate_policy, write_default_policy
from .proxy import create_app
from .pricing_catalog import (
    LITELLM_MODEL_COST_MAP_URL,
    ensure_fresh_pricing_snapshot,
    fetch_litellm_model_cost_map,
    write_litellm_pricing_snapshot,
)
from .reports import build_run_report_payload
from .runner import RunOptions, start_guarded_run
from .service import SentinelService, SessionObservationConflict
from .storage import METADATA_MAX_BYTES, RunStore, json_utf8_size, validate_run_id
from .supervisor import OwnedSupervisor, SupervisorError
from .source_discovery import discover_usage_sources
from . import store_merge
from .env_compat import read_env_alias
from .evidence_runtime import EvidenceRuntime
from .store_resolution import (
    ENV_STORE_DIR,
    StoreResolution,
    StoreResolutionError,
    canonical_global_store_dir,
    onboard_global_store_dir,
    claude_worktree_owner_dir,
    resolve_dashboard_store_dir,
    resolve_read_store_dir,
    resolve_store_dir,
    store_env_dir_value,
)
from .usage_truth import (
    CODEX_REPLAY_QUARANTINE_STATE,
    INSTRUMENTATION_MARKER_EVENT_TYPE,
    is_instrumentation_marker_event,
    local_usage_event_additivity,
    normalized_local_usage_session_id,
    selected_local_session_observation_source_identities,
    usage_truth_table,
)
from .work_events import WORK_EVENT_KINDS, WORK_EVENT_STATUSES, WorkEvent

# pretty_exceptions_show_locals=False: if an exception ever escapes a command,
# it must not dump local variables (paths, payloads) at a first-run user.
app = typer.Typer(
    help="Local-first Agent Work Intelligence for coding agents: usage truth, recorded work, and honest joins.",
    pretty_exceptions_show_locals=False,
)


def _package_version() -> str:
    """The installed agentacct version, or ``0.0.0+source`` for a bare checkout.

    An editable install freezes this at install time, so a version bump is
    observable only after ``uv tool install --force``/reinstall — which is exactly
    when the integration re-sync should refresh the client configs.
    """

    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _dist_version

    try:
        return _dist_version("agentacct")
    except PackageNotFoundError:  # source checkout without installed dist metadata
        return "0.0.0+source"


def _version_callback(value: bool) -> None:
    """Print the installed agentacct version and exit (eager --version option)."""
    if not value:
        return
    print(f"agentacct {_package_version()}")
    raise typer.Exit()


@app.callback()
def _app_main(
    version: Annotated[
        Optional[bool],
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed agentacct version and exit.",
        ),
    ] = None,
) -> None:
    # No docstring: Typer falls back to the app-level help= above, so adding this
    # callback for --version does not change `agentacct --help` output.
    return
cost_app = typer.Typer(help="Cost proxy and usage ledger commands.")
hooks_app = typer.Typer(help="Hook pack commands for agent runtimes.")
policy_app = typer.Typer(help="Project policy commands.")
outcome_app = typer.Typer(help="Outcome evidence commands.")
judge_app = typer.Typer(help="LLM judge package commands.")
value_app = typer.Typer(help="Advisory value scoring commands.")
api_app = typer.Typer(help="Local API commands for sidecar/MCP integrations.")
event_app = typer.Typer(help="Record and list local integration events.")
evidence_app = typer.Typer(help="Inspect and replay the additive multi-source Evidence v2 store.")
connector_app = typer.Typer(help="Import read-only Paperclip, OpenLIT OTLP, and Entire Git evidence.")
control_app = typer.Typer(
    help="Inspect control state and operate only agentacct-owned local attempts; external agents remain read-only."
)
capture_app = typer.Typer(help="Capture metadata-only coding-agent hook observations.")
capabilities_app = typer.Typer(help="Inspect evidence-backed coding-agent capability claims.")
usage_app = typer.Typer(help="Import local client-reported usage from coding agents.")
mcp_app = typer.Typer(help="MCP server commands for agent integrations.")
setup_app = typer.Typer(help="Setup helpers for coding agents and local integrations.")
claude_code_hooks_app = typer.Typer(help="Claude Code hook helpers.")
canonical_app = typer.Typer(
    help="Canonical SQLite store operations: health, maintenance, promotion, and cutover verification."
)
smoke_app = typer.Typer(help="Optional real-agent smoke tests. Not intended for default CI.")
app.add_typer(cost_app, name="cost")
app.add_typer(policy_app, name="policy")
app.add_typer(outcome_app, name="outcome")
app.add_typer(judge_app, name="judge")
app.add_typer(value_app, name="value")
app.add_typer(api_app, name="api")
app.add_typer(event_app, name="event")
app.add_typer(evidence_app, name="evidence")
app.add_typer(connector_app, name="connector")
app.add_typer(control_app, name="control")
app.add_typer(capture_app, name="capture")
app.add_typer(capabilities_app, name="capabilities")
app.add_typer(usage_app, name="usage")
app.add_typer(mcp_app, name="mcp")

# --- task continuation edits + finding attention actions (local write lanes) --
# These replace the retired HTML form handlers: same store mutations, same
# scope rules, driven from the terminal instead of a web page.
task_app = typer.Typer(help="Task continuation edits: link, unlink, rename.")
finding_app = typer.Typer(help="Finding attention actions: list, dispose.")
app.add_typer(task_app, name="task")
app.add_typer(finding_app, name="finding")


def _existing_write_lane_store(store_dir: Path | None) -> Path:
    """Resolve the store for the task/finding write lanes, refusing to invent one.

    A typo'd --store-dir must be a loud error, not a silently fabricated empty
    store that makes real findings appear to vanish (read-lane convention)."""

    resolved = _resolve_cli_store_dir(store_dir).path
    if not Path(resolved).expanduser().exists():
        console.print(
            f"No agentacct store at {resolved}. Check --store-dir (this lane never creates a store)."
        )
        raise typer.Exit(1)
    return resolved


def _continuation_store(store_dir: Path | None):
    from .task_continuations import ContinuationTaskStore

    return ContinuationTaskStore(_existing_write_lane_store(store_dir))


def _resolve_task_id_argument(store_dir: Path | None, task_id: str) -> str:
    """Accept either an internal continuation task id or a public task_… id."""

    if not task_id.startswith("task_"):
        return task_id
    from .api import build_store_task_projection
    from .task_identity import TaskIdentityCodec

    resolved_store = _existing_write_lane_store(store_dir)
    projection = build_store_task_projection(resolved_store)
    resolved = TaskIdentityCodec(resolved_store).resolve(projection, task_id)
    internal = (resolved or {}).get("task_id")
    if not internal:
        console.print(f"No task matches {task_id} in this store.")
        raise typer.Exit(1)
    return str(internal)


@task_app.command("link")
def task_link(
    client: Annotated[str, typer.Option(help="Client of the earlier session (e.g. claude-code).")],
    session: Annotated[str, typer.Option(help="Client session id of the earlier session.")],
    to_client: Annotated[str, typer.Option(help="Client of the continuation session.")],
    to_session: Annotated[str, typer.Option(help="Client session id of the continuation session.")],
    title: Annotated[Optional[str], typer.Option(help="Optional title when a new task is created.")] = None,
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
) -> None:
    """Link two root sessions into one Task (creates/extends/merges as needed)."""

    from .task_continuations import ClientSessionRef, ContinuationTaskError

    try:
        result = _continuation_store(store_dir).link_sessions(
            ClientSessionRef(client=client, client_session_id=session),
            ClientSessionRef(client=to_client, client_session_id=to_session),
            confirmed_by="cli",
            title=title,
        )
    except ValueError as exc:  # ContinuationTaskError subclasses ValueError
        console.print(f"Link failed: {exc}")
        raise typer.Exit(1) from exc
    state = "linked" if result.changed else "already linked"
    console.print(f"Task {result.task_id}: {state}.")


@task_app.command("unlink")
def task_unlink(
    task_id: Annotated[str, typer.Option("--task", help="Task id (internal, or public task_… id).")],
    client: Annotated[str, typer.Option(help="Client of the session to unlink.")],
    session: Annotated[str, typer.Option(help="Client session id to unlink.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
) -> None:
    """Remove one session from a Task."""

    from .task_continuations import ClientSessionRef, ContinuationTaskError

    resolved_task = _resolve_task_id_argument(store_dir, task_id)
    try:
        result = _continuation_store(store_dir).unlink_session(
            resolved_task,
            ClientSessionRef(client=client, client_session_id=session),
            confirmed_by="cli",
        )
    except ValueError as exc:  # ContinuationTaskError subclasses ValueError
        console.print(f"Unlink failed: {exc}")
        raise typer.Exit(1) from exc
    state = "unlinked" if result.changed else "was not linked"
    console.print(f"Task {result.task_id}: session {state}.")


@task_app.command("rename")
def task_rename(
    task_id: Annotated[str, typer.Option("--task", help="Task id (internal, or public task_… id).")],
    title: Annotated[Optional[str], typer.Option(help="New title; omit to clear the override.")] = None,
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
) -> None:
    """Set or clear a Task's title override."""

    from .task_continuations import ContinuationTaskError

    resolved_task = _resolve_task_id_argument(store_dir, task_id)
    try:
        result = _continuation_store(store_dir).rename_task(
            resolved_task, title, confirmed_by="cli"
        )
    except ValueError as exc:  # ContinuationTaskError subclasses ValueError
        console.print(f"Rename failed: {exc}")
        raise typer.Exit(1) from exc
    state = "renamed" if result.changed else "unchanged"
    console.print(f"Task {result.task_id}: {state}.")


@finding_app.command("list")
def finding_list(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
) -> None:
    """Surfaced finding episodes (the scope-quarantined index) with digests."""

    from .api import build_store_task_projection, surfaced_finding_episodes
    from .finding_disposition import finding_target_digest

    resolved_store = _existing_write_lane_store(store_dir)
    projection = build_store_task_projection(resolved_store)
    rows = []
    for episode in surfaced_finding_episodes(projection):
        event = episode.get("failure_event") if isinstance(episode.get("failure_event"), dict) else None
        digest = finding_target_digest(event) if event is not None else None
        if digest is None:
            continue
        rows.append({
            "digest": digest,
            "state": episode.get("disposition_state"),
            "revision": episode.get("revision"),
            "attention_open": bool(episode.get("attention_open")),
            "summary": str((event or {}).get("summary") or (event or {}).get("name") or "")[:120],
        })
    if json_output:
        print(json.dumps({"findings": rows}, indent=2, sort_keys=True))
        return
    if not rows:
        console.print("No surfaced findings.")
        return
    table = Table(show_header=True)
    for column in ("digest", "state", "revision", "open", "summary"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["digest"])[:16],
            str(row["state"] or ""),
            str(row["revision"]),
            "yes" if row["attention_open"] else "no",
            row["summary"],
        )
    console.print(table)


@finding_app.command("dispose")
def finding_dispose(
    digest: Annotated[str, typer.Option(help="Target finding digest (a unique prefix is enough; see `finding list`).")],
    action: Annotated[str, typer.Option(help="mark_reviewed | resolve | reopen | reinstate.")],
    note: Annotated[Optional[str], typer.Option(help="Short note recorded with the action (REQUIRED for resolve).")] = None,
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
) -> None:
    """Record one attention transition for a SURFACED finding.

    Resolves only against the scope-quarantined projection index (exactly what
    `GET /tasks` shows), so a finding hidden from this store's scope cannot be
    dispositioned here — the same refusal the retired form resolver enforced.
    """

    from .api import build_store_task_projection, resolve_finding_episode
    from .finding_disposition import (
        FindingDispositionConflict,
        FindingDispositionNotFound,
        finding_target_digest,
    )
    from .service import SentinelService

    allowed_actions = ("mark_reviewed", "resolve", "reopen", "reinstate")
    if action not in allowed_actions:
        console.print(f"Unknown action {action!r}. Use one of: {', '.join(allowed_actions)}.")
        raise typer.Exit(1)
    if action == "resolve" and not (note or "").strip():
        console.print("Resolving a finding requires --note (say what resolved it).")
        raise typer.Exit(1)
    resolved_store = _existing_write_lane_store(store_dir)
    projection = build_store_task_projection(resolved_store)
    try:
        episode = resolve_finding_episode(projection, digest=digest)
        target = episode.get("failure_event")
        if not isinstance(target, dict):
            raise FindingDispositionNotFound("finding target is unavailable")
        revision = int(episode.get("revision") or 0)
        full_digest = finding_target_digest(target)
        state = str(episode.get("disposition_state") or "")
        attention_open = bool(episode.get("attention_open"))
        already = {"resolve": "resolved", "mark_reviewed": "reviewed"}
        if already.get(action) and state == already[action]:
            console.print(
                f"Finding {str(full_digest)[:16]}: already {already[action]} (revision {revision})."
            )
            return
        if action == "reopen" and state == "open":
            if attention_open:
                console.print(
                    f"Finding {str(full_digest)[:16]}: already open (revision {revision})."
                )
                return
            # Superseded-but-open: state says open, attention is off. reopen
            # cannot bring it back — that is exactly what reinstate is for.
            console.print(
                f"Finding {str(full_digest)[:16]}: state is open but attention is off "
                "(a later pass superseded it). Use --action reinstate to bring it back."
            )
            raise typer.Exit(1)
        if action == "reinstate" and attention_open:
            # Idempotent: reinstating an already-attended finding must not
            # append another ledger row on every retry.
            console.print(
                f"Finding {str(full_digest)[:16]}: already under attention (revision {revision})."
            )
            return
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
        idempotency_key = f"cli:finding:{full_digest}:{revision}:{action}"
        service = SentinelService(resolved_store)
        replay = service.replay_finding_disposition(
            action=action,
            expected_revision=revision,
            note=note,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            console.print(f"Finding {str(full_digest)[:16]}: {action} already recorded (replay).")
            return
        service.record_finding_disposition(
            target_event=target,
            action=action,
            expected_revision=revision,
            note=note,
            idempotency_key=idempotency_key,
            task_scope=task_scope,
            transport="cli",
        )
    except FindingDispositionNotFound as exc:
        console.print(f"Finding not found: {exc}")
        raise typer.Exit(1) from exc
    except FindingDispositionConflict as exc:
        console.print(f"Finding changed or the action is no longer valid: {exc}")
        raise typer.Exit(1) from exc
    console.print(f"Finding {str(full_digest)[:16]}: {action} recorded.")

app.add_typer(setup_app, name="setup")
hooks_app.add_typer(claude_code_hooks_app, name="claude-code")
app.add_typer(hooks_app, name="hooks")
app.add_typer(canonical_app, name="canonical")
app.add_typer(smoke_app, name="smoke")
console = Console()


def _parse_duration(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip().lower()
    if value.endswith("ms"):
        return float(value[:-2]) / 1000
    if value.endswith("s"):
        return float(value[:-1])
    if value.endswith("m"):
        return float(value[:-1]) * 60
    if value.endswith("h"):
        return float(value[:-1]) * 3600
    return float(value)


def _is_ignored_by_git(project_dir: Path, relative_path: str) -> bool:
    gitignore = project_dir / ".gitignore"
    if not gitignore.exists():
        return False
    patterns = {line.strip().rstrip("/") for line in gitignore.read_text().splitlines() if line.strip() and not line.strip().startswith("#")}
    return relative_path in patterns or relative_path.rstrip("/") in patterns


# The entries keep the frozen ".agent-sentinel/" store dir name (pre-rename,
# kept forever); only the surrounding branding renames.
AGENT_CHRONICLE_GITIGNORE_ENTRIES = [
    ".agent-sentinel/state/",
    ".agent-sentinel/*.db",
    ".agent-sentinel/*.jsonl",
    ".env",
    ".env.local",
]

DEFAULT_AGENT_SECTION_MARKER = "## agentacct"
CLAUDE_CODE_AGENT_SECTION_MARKER = "# agentacct"
# Pre-rename headings recognized forever: an existing "Agent Chronicle" or
# "Agent Sentinel" section in a user's CLAUDE.md/AGENTS.md must keep suppressing
# duplicate appends.
LEGACY_AGENT_SECTION_MARKERS = {
    "## Agent Chronicle",
    "# Agent Chronicle",
    "## Agent Sentinel",
    "# Agent Sentinel",
}
AGENT_SECTION_MARKERS = {DEFAULT_AGENT_SECTION_MARKER, CLAUDE_CODE_AGENT_SECTION_MARKER} | LEGACY_AGENT_SECTION_MARKERS

AGENT_INSTRUCTION_TARGETS = {
    "claude-code": "CLAUDE.md",
    "codex": "AGENTS.md",
    "generic": "AGENTS.md",
    "hermes": "AGENTS.md",
    "opencode": "AGENTS.md",
    "openclaw": "AGENTS.md",
}

MCP_SETUP_AGENTS = {"claude-code", "codex", "generic", "hermes", "opencode", "openclaw"}


def _append_missing_gitignore_entries(project_dir: Path) -> list[str]:
    gitignore = project_dir / ".gitignore"
    existing_text = gitignore.read_text() if gitignore.exists() else ""
    existing = {line.strip() for line in existing_text.splitlines() if line.strip()}
    missing = [entry for entry in AGENT_CHRONICLE_GITIGNORE_ENTRIES if entry not in existing]
    if missing:
        prefix = "" if not existing_text or existing_text.endswith("\n") else "\n"
        block = "# agentacct local state and secrets\n" + "\n".join(missing) + "\n"
        gitignore.write_text(existing_text + prefix + block)
    return missing


def _agent_section_marker(agent: str) -> str:
    return CLAUDE_CODE_AGENT_SECTION_MARKER if agent == "claude-code" else DEFAULT_AGENT_SECTION_MARKER


def _has_agent_section(text: str) -> bool:
    return any(line.strip() in AGENT_SECTION_MARKERS for line in text.splitlines())


def _agent_section(agent: str) -> str:
    return f"""{_agent_section_marker(agent)}

Use agentacct in observe-only mode.

- Prefer `agentacct run -- <command>` for tracked local commands.
- If a project-local store exists, keep `.agent-sentinel/state/` and local env files gitignored (a global install keeps its store outside any repo, so there is nothing to ignore).
- Do not connect provider API keys unless the user explicitly asks for provider cost proxy/forwarding.
- If the agentacct MCP tools are available, use them as the normal workflow ledger:
  - call `agentacct_attach_client_context` when local session, parent session, turn, request, or message IDs are known;
  - open a section with `agentacct_record_section` (`section_status=started`) before meaningful work; use `section_status=checkpoint` while it progresses;
  - call `agentacct_record_agent_usage_debug` when the client exposes visible token/cost usage, or with `reporting_basis=unavailable` when it does not;
  - record machine-check evidence after tests/builds with `agentacct_record_machine_check` or `agentacct_record_event`;
  - finish the section with `section_status=completed` or `section_status=blocked` and include objective evidence.
- Keep MCP/event claims separate from usage/cost claims. MCP events prove that the agent recorded work and semantic context; agent usage debug events are comparison evidence only. A client-reported token usage claim requires a supported local usage importer or explicit client JSON output.
- After meaningful work, show the run/report path or event summary and summarize objective evidence: tests, build result, changed files, tool calls, token/cost data if actually observed, and repeated errors.
"""


def _install_agent_instructions(project_dir: Path, agent: str) -> Path:
    if agent not in AGENT_INSTRUCTION_TARGETS:
        raise ValueError(f"unsupported agent target: {agent}")
    path = project_dir / AGENT_INSTRUCTION_TARGETS[agent]
    text = path.read_text() if path.exists() else ""
    if not _has_agent_section(text):
        prefix = "" if not text or text.endswith("\n") else "\n\n"
        path.write_text(text + prefix + _agent_section(agent))
    return path


# One --store-dir help string for every store-consuming command (decision 4a).
_STORE_DIR_HELP = (
    "State directory. Defaults to the project store (.agent-sentinel/state, found by walking up from the current "
    "directory; Claude worktrees resolve to the owning project). Override with --store-dir or "
    "AGENTACCT_STORE_DIR. Fails if no project store exists."
)

_DASHBOARD_STORE_DIR_HELP = (
    "State directory for the dashboard. With no override, an installed machine-wide "
    "~/.agent-sentinel-global/state store opens as the All projects product view; otherwise agentacct uses the "
    "current project store. Pass a project .agent-sentinel/state path for an explicit workspace view. "
    "AGENTACCT_STORE_DIR (or its pre-rename aliases) overrides both defaults."
)

# One --allow-host help string for every local HTTP server command.
_ALLOW_HOST_HELP = (
    "Extra hostname accepted by the Host/Origin guard (repeatable; hostname only, no port). "
    "localhost, 127.0.0.1, and [::1] are always allowed; other Hosts and cross-site "
    "browser Origins are rejected with 403."
)


def _resolve_cli_store_dir(store_dir: Path | str | None) -> StoreResolution:
    """Resolve the store for a CLI command, or exit 2 with an actionable error.

    The worktree notice and resolution errors go to stderr so `--json` output
    on stdout stays machine-parseable.
    """
    try:
        resolution = resolve_store_dir(store_dir)
    except StoreResolutionError as exc:
        print(str(exc), file=sys.stderr)
        raise typer.Exit(2) from exc
    if resolution.worktree_remapped:
        print(f"Claude worktree detected; using the owning project store: {resolution.path}", file=sys.stderr)
    return resolution


def _resolve_read_cli_store_dir(store_dir: Path | str | None) -> StoreResolution:
    """Resolve the store for a read-only display command (tui / now / limits).

    Like :func:`_resolve_cli_store_dir` but project-first-then-global: with no
    ``--store-dir`` / env override and no project store on the walk-up, it falls back
    to the machine-wide store so a global-by-default install's ``agentacct tui`` just
    works from any directory instead of exiting 2. The friendly worktree notice and
    the actionable no-store error (when there is no global store either) are kept.
    """
    try:
        resolution = resolve_read_store_dir(store_dir)
    except StoreResolutionError as exc:
        print(str(exc), file=sys.stderr)
        raise typer.Exit(2) from exc
    if resolution.worktree_remapped:
        print(f"Claude worktree detected; using the owning project store: {resolution.path}", file=sys.stderr)
    return resolution


def _resolve_dashboard_cli_store_dir(store_dir: Path | str | None) -> StoreResolution:
    """Resolve the product dashboard, preserving the CLI's friendly errors."""

    try:
        resolution = resolve_dashboard_store_dir(store_dir)
    except StoreResolutionError as exc:
        print(str(exc), file=sys.stderr)
        raise typer.Exit(2) from exc
    if resolution.worktree_remapped:
        print(f"Claude worktree detected; using the owning project store: {resolution.path}", file=sys.stderr)
    return resolution


def _resolve_scratch_store_dir(store_dir: Path | str | None, *, label: str) -> tuple[Path, bool]:
    """Store resolution for demo-style commands that write throwaway data.

    Explicit flag and AGENTACCT_STORE_DIR are honored; otherwise a
    THROWAWAY temporary store is created (never the project ledger — demo
    events in a production store are junk data). Returns (path, is_temporary).
    """
    if store_dir is not None or store_env_dir_value(os.environ):
        return _resolve_cli_store_dir(store_dir).path, False
    return Path(tempfile.mkdtemp(prefix=f"agent-chronicle-{label}-")), True


_NO_RUNS_MESSAGE = (
    "No runs recorded in this store yet. Create one with `agentacct demo` or `agentacct run -- <command>`."
)

_EMPTY_EVENT_LEDGER_HINT = (
    "Next step: import client usage with `agentacct usage import-local`, or record MCP sections from your "
    "coding agent (see `agentacct mcp doctor`)."
)


@contextmanager
def _friendly_run_lookup_errors(run_id: str | None = None) -> Iterator[None]:
    """Turn run-store lookup and control-command failures into one friendly line and exit(1).

    First contact with `report`/`judge`/`value`/`pause`/`resume`/`kill` right
    after `init` must never be a stack dump: an empty store, an unknown or
    malformed run id, an ownership refusal, and a control command targeting a
    process group that already exited or belongs to another process each get a
    single actionable sentence on stderr (mirroring `_resolve_cli_store_dir`),
    keeping stdout `--json`-safe. `run_id` (when passed) lets the control-command
    and malformed-id messages name the run.
    """
    try:
        yield
    except ProcessLookupError as exc:
        # os.killpg on an already-exited process group (ESRCH): e.g. `demo`
        # (its run exits in ~0.2s) then pause/resume/kill that run.
        target = f"Run {run_id}" if run_id else "That run"
        print(f"{target} is not running (its process already exited).", file=sys.stderr)
        raise typer.Exit(1) from exc
    except FileNotFoundError as exc:
        message = str(exc)
        if message == "no runs found":
            print(_NO_RUNS_MESSAGE, file=sys.stderr)
        elif message.startswith("unknown run_id:"):
            print(f"{message}. List recorded runs with `agentacct runs`.", file=sys.stderr)
        else:
            print(message, file=sys.stderr)
        raise typer.Exit(1) from exc
    except PermissionError as exc:
        if exc.errno is not None:
            # An OS-level EPERM from killpg (a stale/foreign process group).
            # assert_owned already blocks non-agentacct runs, so this is a pgid
            # the OS won't let us signal — name the run instead of dumping a
            # context-free "[Errno 1] Operation not permitted".
            target = f"run {run_id}" if run_id else "that run"
            print(f"Cannot signal {target}: {exc}. Its recorded process group is not signalable.", file=sys.stderr)
        else:
            # Ownership refusals keep their existing message semantics.
            print(str(exc), file=sys.stderr)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        message = str(exc)
        if message.startswith("invalid run_id:"):
            shown = run_id if run_id is not None else message[len("invalid run_id: ") :].strip("'\"")
            shown = str(shown)
            if len(shown) > 40:
                shown = shown[:37] + "..."
            print(
                f"Not a valid run id: {shown!r}. List recorded runs with `agentacct runs`.",
                file=sys.stderr,
            )
            raise typer.Exit(1) from exc
        raise


def _display_path(path: Path) -> Path:
    resolved = path.resolve()
    cwd = Path.cwd().resolve()
    if resolved.is_relative_to(cwd):
        return Path(".") / resolved.relative_to(cwd)
    return path


def _limited_text(value: str | None, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    if len(value) > max_length:
        raise typer.BadParameter(f"--{field.replace('_', '-')} must be <= {max_length} characters")
    return value


def _current_agentacct_executable() -> str | None:
    candidate = Path(sys.argv[0]).expanduser()
    # agentacct is the published console command; "agent-chronicle" /
    # "agent-sentinel" are pre-rename aliases of the same entry point.
    if candidate.name not in ("agentacct", "agent-chronicle", "agent-sentinel"):
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return str(resolved) if resolved.exists() else None


def _managed_runtime(
    store_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    project_dir: Path | None = None,
) -> RuntimeManager:
    executable = (
        _current_agentacct_executable()
        or shutil.which("agentacct")
        or shutil.which("agent-chronicle")
        or shutil.which("agent-sentinel")
    )
    if executable is None:
        raise typer.BadParameter(
            "the managed runtime needs an installed agentacct console script; "
            "run this command from the environment where agentacct is installed"
        )
    return RuntimeManager(
        store_dir,
        executable=executable,
        host=host,
        port=port,
        cwd=project_dir,
    )


def _runtime_ingestion_health(store_dir: Path) -> tuple[dict[str, Any], bool]:
    """Resolve health plus the conservative external-watcher decision."""

    return IngestionHealthStore(store_dir).runtime_watcher_snapshot()


def _merge_claude_project_settings(project_dir: Path) -> tuple[Path, str]:
    """Merge agentacct's generated hook/env block without replacing user keys."""

    _hook_path, example_path = claude_code_hook_paths(project_dir)
    if not example_path.exists():
        raise RuntimeManagerError("Claude Code hook settings example was not generated")
    target = project_dir / ".claude" / "settings.local.json"
    return _merge_claude_settings_from_example(example_path, target)


def _merge_claude_settings_from_example(example_path: Path, target: Path) -> tuple[Path, str]:
    """Non-destructively merge a generated hook/env example into a settings file.

    Shared by the project install (``.claude/settings.local.json``) and the
    default-global install (user-level ``~/.claude/settings.json``): an env key is
    never overwritten when the user set a different value, hook rows are deduped
    by fingerprint, and the write is atomic + 0600.
    """
    try:
        generated = json.loads(example_path.read_text(encoding="utf-8"))
        current = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeManagerError(
            f"Claude Code settings could not be merged safely: {type(exc).__name__}. "
            f"Review {example_path} and {target}."
        ) from exc
    if not isinstance(generated, dict) or not isinstance(current, dict):
        raise RuntimeManagerError("Claude Code settings must be JSON objects; no settings were overwritten")
    env = current.setdefault("env", {})
    hooks = current.setdefault("hooks", {})
    if not isinstance(env, dict) or not isinstance(hooks, dict):
        raise RuntimeManagerError("Claude Code settings env/hooks must be objects; no settings were overwritten")
    changed = not target.exists()
    desired_env = generated.get("env") if isinstance(generated.get("env"), dict) else {}
    for key, value in desired_env.items():
        if key in env and env[key] != value:
            raise RuntimeManagerError(
                f"Claude Code setting env.{key} already has a different value; agentacct left it unchanged. "
                f"Merge {example_path} manually."
            )
        if key not in env:
            changed = True
        env[key] = value
    # statusLine is an OPTIONAL, cosmetic add-on (the terminal-CLI rate-limit bar),
    # NOT part of the required record-your-work recipe. So — unlike env/hooks — a
    # conflict must NEVER be fatal: a user who already runs their own statusLine
    # (ccusage, powerline, a custom script) keeps it, and env + hooks still merge.
    # We only install ours when the user has none.
    desired_statusline = generated.get("statusLine")
    if isinstance(desired_statusline, dict):
        current_statusline = current.get("statusLine")
        if current_statusline is None:
            current["statusLine"] = desired_statusline
            changed = True
        # else: absent-vs-ours already handled; a user's own different statusLine
        # is left untouched (they can adopt agentacct's manually).
    desired_hooks = generated.get("hooks") if isinstance(generated.get("hooks"), dict) else {}
    for event_name, rows in desired_hooks.items():
        if not isinstance(rows, list):
            continue
        existing_rows = hooks.setdefault(event_name, [])
        if not isinstance(existing_rows, list):
            raise RuntimeManagerError(
                f"Claude Code setting hooks.{event_name} is not an array; agentacct left it unchanged"
            )
        fingerprints = {
            json.dumps(row, sort_keys=True, separators=(",", ":"))
            for row in existing_rows
            if isinstance(row, dict)
        }
        for row in rows:
            fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"))
            if fingerprint not in fingerprints:
                existing_rows.append(row)
                fingerprints.add(fingerprint)
                changed = True
    target.parent.mkdir(parents=True, exist_ok=True)
    if changed or not target.exists():
        payload = (json.dumps(current, indent=2) + "\n").encode("utf-8")
        temporary = target.parent / f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    return target, "updated" if changed else "unchanged"


def _resolve_mcp_command(value: str | None) -> str:
    explicit = _limited_text(value, field="mcp_command", max_length=512)
    if explicit:
        return explicit
    if shutil.which("agentacct"):
        return "agentacct"
    if shutil.which("agent-chronicle"):
        # Transition-alias installs: the pre-rename binary name is on PATH.
        return "agent-chronicle"
    if shutil.which("agent-sentinel"):
        # Pre-rename installs: only the oldest binary name is on PATH.
        return "agent-sentinel"
    return _current_agentacct_executable() or "agentacct"


def _resolve_absolute_mcp_command() -> str:
    """An ABSOLUTE path to the agentacct binary for user-scope registrations.

    GUI-launched clients (Claude Code desktop, Codex.app) do NOT inherit the
    shell PATH, so a bare ``agentacct`` command would fail to launch there. The
    global install bakes the absolute path (like the manual runbook's
    ``command -v agentacct``) instead. Falls back to the bare name only if no
    absolute path can be found — never registers an empty command.
    """
    for name in ("agentacct", "agent-chronicle", "agent-sentinel"):
        found = shutil.which(name)
        if found:
            return found
    return _current_agentacct_executable() or "agentacct"


def _parse_metadata_json(value: str | None) -> dict[str, object]:
    if value is None or value == "":
        return {}
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(f"non-standard JSON constant is not allowed: {constant}")),
        )
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--metadata-json must be valid JSON: {exc.msg}") from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter("--metadata-json must decode to a JSON object")
    try:
        # Real UTF-8 bytes, measured by the same helper the HTTP and MCP lanes
        # use: the escaped encoding billed CJK text 2x, so the same payload was
        # accepted on one surface and rejected on another.
        size = json_utf8_size(parsed, allow_nan=False)
    except ValueError as exc:
        raise typer.BadParameter(f"--metadata-json must be strict JSON: {exc}") from exc
    if size > METADATA_MAX_BYTES:
        raise typer.BadParameter(f"--metadata-json must be <= {METADATA_MAX_BYTES} bytes when JSON encoded")
    return parsed


def _optional_non_negative_int(value: int | None, *, field: str) -> int | None:
    if value is not None and value < 0:
        raise typer.BadParameter(f"--{field.replace('_', '-')} must be >= 0")
    return value


def _optional_non_negative_float(value: float | None, *, field: str) -> float | None:
    if value is not None and (not math.isfinite(value) or value < 0):
        raise typer.BadParameter(f"--{field.replace('_', '-')} must be a finite value >= 0")
    return value


def _validated_optional_run_id(run_id: str | None) -> str | None:
    if run_id is None:
        return None
    try:
        validate_run_id(run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return run_id


def _safe_usd(value: object) -> str:
    try:
        number = float(0.0 if value is None else value)  # type: ignore[arg-type]
    except (OverflowError, TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number):
        number = 0.0
    return f"${number:.6f}"


def _metadata_summary(metadata: object, *, max_length: int = 80) -> str:
    if not isinstance(metadata, dict) or not metadata:
        return ""
    summary = metadata.get("summary")
    text = str(summary) if summary is not None else json.dumps(metadata, sort_keys=True, ensure_ascii=False)
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def _print_counter(title: str, values: object) -> None:
    print(f"{title}:")
    if not isinstance(values, dict) or not values:
        print("  none")
        return
    for key, count in sorted(values.items(), key=lambda item: (-int(item[1]), str(item[0]))):
        print(f"  {key}: {count}")


def _mcp_store_dir_for_project(project_dir: Path, explicit_store_dir: Path | None = None) -> Path:
    """Default store path for MCP config written for ``project_dir``.

    Mirrors ``resolve_store_dir``'s worktree semantics: a temporary Claude Code
    worktree at ``<owner>/.claude/worktrees/<name>`` belongs to its owning
    repository, so config written from inside one must never embed the
    worktree's vanishing store path (it would merge to main and silently
    re-create phantom stores after worktree cleanup). The worktree's own
    EXISTING store still wins, consistent with the resolver.
    """
    if explicit_store_dir is not None:
        return explicit_store_dir.expanduser().resolve()
    project_root = project_dir.resolve()
    own_store = project_root / ".agent-sentinel" / "state"
    if not own_store.is_dir():
        owner_dir = claude_worktree_owner_dir(project_root)
        if owner_dir is not None:
            return (owner_dir.resolve() / ".agent-sentinel" / "state").resolve()
    return own_store.resolve()


def _onboarding_project_dir(project_dir: Path) -> tuple[Path, Path | None]:
    """Return the one stable project root every onboarding step must share.

    ``init_project`` already remaps temporary Claude Code worktrees to their
    owning repository when the worktree has no explicit agentacct store.  The
    composed onboarding flow must make that decision *before* it derives the
    store path, installs hooks, imports usage, or starts the runtime; otherwise
    configuration lands in the owner while activation/runtime state lands in
    the disposable worktree.
    """

    resolved = project_dir.expanduser().resolve()
    if (resolved / ".agent-sentinel" / "state").is_dir():
        return resolved, None
    owner = claude_worktree_owner_dir(resolved)
    if owner is None:
        return resolved, None
    return owner.resolve(), resolved


def _print_claude_worktree_store_hint(project_dir: Path, *, command: str = "agentacct") -> None:
    owner_dir = claude_worktree_owner_dir(project_dir)
    if owner_dir is None:
        return
    stable_store = owner_dir / ".agent-sentinel" / "state"
    console.print("Claude Code worktree detected.")
    console.print("For a stable ledger, keep agentacct state in the owning project instead of this temporary worktree:")
    command_parts = [
        "agentacct",
        "setup",
        "mcp",
        "--agent",
        "claude-code",
        "--project-dir",
        shlex.quote(str(owner_dir)),
        "--store-dir",
        shlex.quote(str(stable_store)),
    ]
    if command != "agentacct":
        command_parts.extend(["--mcp-command", shlex.quote(command)])
    command_parts.append("--write")
    print(" ".join(command_parts))


def _mcp_server_config(store_dir: Path | str, *, command: str = "agentacct") -> dict[str, object]:
    return {"command": command, "args": ["mcp", "serve", "--store-dir", str(store_dir)]}


def _claude_mcp_json(store_dir: Path | str, *, command: str = "agentacct") -> str:
    return json.dumps({"mcpServers": {"agentacct": _mcp_server_config(store_dir, command=command)}}, indent=2) + "\n"


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _codex_mcp_toml_block(store_dir: Path | str, *, command: str = "agentacct", env: dict[str, str] | None = None) -> str:
    args = ["mcp", "serve", "--store-dir", str(store_dir)]
    block = (
        "# agentacct MCP\n"
        "[mcp_servers.agentacct]\n"
        f"command = {_toml_string(command)}\n"
        f"args = [{', '.join(_toml_string(arg) for arg in args)}]\n"
    )
    if env:
        # Inline table keeps the registration a single section, so a later
        # upsert replaces it wholesale (no orphanable env sub-table).
        pairs = ", ".join(f"{_toml_string(key)} = {_toml_string(str(value))}" for key, value in env.items())
        block += f"env = {{ {pairs} }}\n"
    return block


# One note, printed by BOTH MCP config writers, when a pre-rename registration
# carries settings this writer would not generate (custom command/args/store).
_PRE_RENAME_CUSTOM_SETTINGS_NOTE = (
    'Pre-rename "agent-sentinel" registration with custom settings found; it keeps '
    "working as-is — re-run setup mcp after removing it if you want the new name."
)


def _pre_rename_registration_matches_generated(existing: object, generated: dict[str, object]) -> bool:
    """True when a pre-rename registration is the standard install this writer
    would generate today: same args (store dir included) and same command,
    ignoring the old/new binary NAME itself (the transition alias makes them
    the same entry point) and any env table (env is carried on migration).
    Anything else is the user's custom configuration and is never migrated."""
    if not isinstance(existing, dict):
        return False
    if set(existing) - {"command", "args", "env"}:
        return False
    env = existing.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(value, str) for value in env.values()):
        return False
    if list(existing.get("args") or []) != list(generated["args"]):  # type: ignore[arg-type]
        return False
    generated_command = str(generated["command"])
    acceptable = {generated_command}
    # The transition aliases run the same entry point, so a standard pre-rename
    # registration differing only in the binary NAME still matches.
    if generated_command.endswith("agentacct"):
        stem = generated_command[: -len("agentacct")]
        acceptable.add(stem + "agent-chronicle")
        acceptable.add(stem + "agent-sentinel")
    return existing.get("command") in acceptable


def _registration_env(registration: object) -> dict[str, str]:
    if isinstance(registration, dict):
        env = registration.get("env")
        if isinstance(env, dict):
            return {str(key): str(value) for key, value in env.items()}
    return {}


def _write_claude_mcp_config(project_dir: Path, config_store_dir: Path | str, *, command: str = "agentacct") -> tuple[Path, str]:
    config_path = project_dir / ".mcp.json"
    existing: dict[str, object] = {}
    if config_path.exists():
        existing = json.loads(config_path.read_text())
    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise typer.BadParameter("existing .mcp.json has non-object mcpServers")
    generated = _mcp_server_config(config_store_dir, command=command)
    old_registration = servers.get("agent-sentinel")
    if old_registration is not None:
        if not _pre_rename_registration_matches_generated(old_registration, generated):
            # The user's custom pre-rename settings (command/args/store/env)
            # must never be silently replaced — and writing the new key
            # alongside would register two servers against different stores.
            console.print(_PRE_RENAME_CUSTOM_SETTINGS_NOTE)
            return config_path, "skipped"
    # Collapse every prior-generation registration into the one new-name key,
    # carrying env forward (`agent-sentinel` is a protected pre-rename name,
    # guarded above; `agent-chronicle` was this tool's OWN prior name, so it is
    # always replaced). Leaving any behind would launch a second server
    # against a possibly-different store (split ledger).
    carried_env: dict[str, str] = {}
    for old_key in ("agent-sentinel", "agent-chronicle"):
        reg = servers.pop(old_key, None)
        carried_env = {**carried_env, **_registration_env(reg)}
    if carried_env:
        generated["env"] = carried_env
    servers["agentacct"] = generated
    config_path.write_text(json.dumps(existing, indent=2) + "\n")
    return config_path, "wrote"


def _write_codex_mcp_config(project_dir: Path, config_store_dir: Path | str, *, command: str = "agentacct") -> tuple[Path, str]:
    return _write_codex_mcp_config_at(project_dir / ".codex" / "config.toml", config_store_dir, command=command)


def _write_codex_mcp_config_at(config_path: Path, config_store_dir: Path | str, *, command: str = "agentacct") -> tuple[Path, str]:
    generated = _mcp_server_config(config_store_dir, command=command)
    env: dict[str, str] = {}
    if config_path.exists():
        try:
            parsed = tomllib.loads(config_path.read_text())
        except tomllib.TOMLDecodeError:
            # Unreadable TOML is already broken for codex; the textual upsert
            # below neither improves nor worsens the custom-settings question.
            parsed = {}
        parsed_servers = parsed.get("mcp_servers")
        if isinstance(parsed_servers, dict):
            old_registration = parsed_servers.get("agent-sentinel")
            if old_registration is not None and not _pre_rename_registration_matches_generated(old_registration, generated):
                # Same rule as the .mcp.json writer: custom pre-rename
                # settings are the user's; write nothing over them.
                console.print(_PRE_RENAME_CUSTOM_SETTINGS_NOTE)
                return config_path, "skipped"
            # Carry env into the replacement block: from the standard old
            # block being migrated, and from an existing new-name block being
            # re-written (its env sub-table would otherwise be dropped by the
            # section removal below). New-name env wins on key conflicts.
            env = {
                **_registration_env(old_registration),
                **_registration_env(parsed_servers.get("agent-chronicle")),
                **_registration_env(parsed_servers.get("agentacct")),
            }
    block = _codex_mcp_toml_block(config_store_dir, command=command, env=env)
    action = _upsert_toml_block(
        config_path,
        "[mcp_servers.agentacct]",
        block,
        # Every comment generation recognized so migrating an old block also
        # removes its marker comment.
        comment_markers={"# agentacct MCP", "# Agent Chronicle MCP", "# Agent Sentinel MCP"},
        # Quoted-TOML equivalence for the new name, plus every pre-rename
        # header (agent-chronicle was this tool's own prior name) so a re-run
        # MIGRATES old blocks instead of adding a duplicate registration.
        equivalent_section_headers={
            '[mcp_servers."agentacct"]',
            "[mcp_servers.agent-chronicle]",
            '[mcp_servers."agent-chronicle"]',
            "[mcp_servers.agent-sentinel]",
            '[mcp_servers."agent-sentinel"]',
        },
    )
    return config_path, action


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace) at ``mode``.

    Used for user-level config that a concurrently-running client may also
    rewrite (notably ``~/.claude.json``): a torn read-modify-write would corrupt
    the client's own state, so the swap is atomic and the file mode is preserved.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _write_user_claude_mcp_config(
    config_path: Path, config_store_dir: Path | str, *, command: str = "agentacct"
) -> tuple[Path, str]:
    """Merge the agentacct server into the USER-level ``~/.claude.json``.

    Unlike the project ``.mcp.json`` writer, this file is Claude Code's own
    global state (caches, projects, account): we merge into the top-level
    ``mcpServers`` object, PRESERVE every other top-level key AND every extra key
    on the ``agentacct`` entry (e.g. ``alwaysLoad``), and only set
    ``type``/``command``/``args``. User-scope entries use ``type: "stdio"``. The
    write is atomic + 0600 because a live Claude session may rewrite this file.
    """
    existing: dict[str, object] = {}
    mode = 0o600
    if config_path.exists():
        mode = (config_path.stat().st_mode & 0o777) or 0o600
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise typer.BadParameter(
                f"{config_path} could not be read as JSON ({type(exc).__name__}); refusing to overwrite it. "
                "Fix or move the file, then re-run."
            ) from exc
        if not isinstance(loaded, dict):
            raise typer.BadParameter(f"{config_path} is not a JSON object; refusing to modify it.")
        existing = loaded
    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise typer.BadParameter(f"{config_path} has a non-object mcpServers; refusing to modify it.")

    args = ["mcp", "serve", "--store-dir", str(config_store_dir)]
    generated_for_match = {"command": command, "args": args}
    # Collapse this tool's own prior name (always safe to replace); carry env.
    # A CUSTOM pre-rename (old-name) entry is the user's — leave it + warn
    # rather than clobber it (they may still run a second server; documented).
    carried_env = dict(_registration_env(servers.pop("agent-chronicle", None)))
    old_sentinel = servers.get("agent-sentinel")
    if old_sentinel is not None:
        if _pre_rename_registration_matches_generated(old_sentinel, generated_for_match):
            carried_env.update(_registration_env(servers.pop("agent-sentinel")))
        else:
            console.print(_PRE_RENAME_CUSTOM_SETTINGS_NOTE)

    entry_obj = servers.get("agentacct")
    entry: dict[str, object] = dict(entry_obj) if isinstance(entry_obj, dict) else {}
    action = "updated" if entry else "wrote"
    merged_env = {**carried_env, **_registration_env(entry)}
    entry["type"] = "stdio"
    entry["command"] = command
    entry["args"] = args
    if merged_env:
        entry["env"] = merged_env
    servers["agentacct"] = entry

    _atomic_write_text(config_path, json.dumps(existing, indent=2) + "\n", mode=mode)
    return config_path, action


def _print_claude_mcp_setup(config_store_dir: Path | str, *, command: str = "agentacct") -> None:
    quoted_store_dir = shlex.quote(str(config_store_dir))
    quoted_command = shlex.quote(command)
    console.print("Claude Code")
    console.print("Copy/paste command:")
    print(f"claude mcp add --scope project agentacct -- {quoted_command} mcp serve --store-dir {quoted_store_dir}")
    console.print("Project .mcp.json preview:")
    print(_claude_mcp_json(config_store_dir, command=command).rstrip())


def _print_codex_mcp_setup(config_store_dir: Path | str, *, command: str = "agentacct") -> None:
    quoted_store_dir = shlex.quote(str(config_store_dir))
    quoted_command = shlex.quote(command)
    console.print("Codex")
    console.print("Copy/paste command:")
    print(f"codex mcp add agentacct -- {quoted_command} mcp serve --store-dir {quoted_store_dir}")
    console.print("Codex config.toml preview:")
    print(_codex_mcp_toml_block(config_store_dir, command=command).rstrip())


def _print_stale_registration_remediation(agent: str) -> None:
    """Tell the user to remove any leftover pre-rename server before adding agentacct.

    A registration left over from the ``agent-sentinel`` / ``agent-chronicle``
    era points at a command name that no longer ships (only ``agentacct`` does),
    so the host tries to launch a missing binary — an ENOENT that reads to the
    client as a crashed MCP server. Removing the dead entry first is the fix.
    """
    console.print(
        "First remove any stale pre-rename server — a leftover agent-sentinel/agent-chronicle "
        "entry launches a command that no longer exists (ENOENT), which the client reports as a crash:"
    )
    if agent == "generic":
        console.print(
            "In the agent's MCP config, delete any server named 'agent-sentinel' or 'agent-chronicle' "
            "(only 'agentacct' ships now), then add the definition below."
        )
        return
    print(f"{agent} mcp remove agent-sentinel")
    print(f"{agent} mcp remove agent-chronicle")


def _print_agent_mcp_preview(agent: str, config_store_dir: Path | str, *, command: str = "agentacct") -> None:
    quoted_store_dir = shlex.quote(str(config_store_dir))
    quoted_command = shlex.quote(command)
    if agent == "generic":
        console.print("Generic MCP-capable agent")
        _print_stale_registration_remediation(agent)
        console.print("Use this stdio MCP server definition in the agent's project-local MCP config:")
        print(_claude_mcp_json(config_store_dir, command=command).rstrip())
        console.print("Command form:")
        print(f"{quoted_command} mcp serve --store-dir {quoted_store_dir}")
        return
    if agent == "hermes":
        console.print("Hermes")
        console.print("Hermes manages MCP servers in the active Hermes profile, not in this repo.")
        _print_stale_registration_remediation(agent)
        console.print("Copy/paste command:")
        print(f"hermes mcp add agentacct --command {quoted_command} --args mcp serve --store-dir {quoted_store_dir}")
        return
    if agent == "opencode":
        console.print("OpenCode")
        _print_stale_registration_remediation(agent)
        console.print("Copy/paste command:")
        print(f"opencode mcp add agentacct -- {quoted_command} mcp serve --store-dir {quoted_store_dir}")
        return
    if agent == "openclaw":
        console.print("OpenClaw")
        _print_stale_registration_remediation(agent)
        console.print("Copy/paste command:")
        print(
            "openclaw mcp add agentacct "
            f"--command {quoted_command} --arg mcp --arg serve --arg --store-dir --arg {quoted_store_dir}"
        )
        return
    raise ValueError(f"unsupported MCP agent target: {agent}")


def _upsert_toml_block(
    path: Path,
    section_header: str,
    block: str,
    comment_markers: set[str] | None = None,
    equivalent_section_headers: set[str] | None = None,
) -> str:
    """Replace ALL recognized sections (and their ``[header.child]``
    sub-tables) with exactly one new block, inserted where the first removed
    section stood. Removing every recognized section — old-name AND new-name —
    is what keeps a both-generations config from migrating into a duplicate
    ``[mcp_servers.agent-chronicle]`` table (invalid TOML) or leaving a stale
    old-name registration alive (split ledger)."""
    existing = path.read_text() if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = existing.splitlines(keepends=True)
    headers = {section_header, *(equivalent_section_headers or set())}
    # "[mcp_servers.agent-chronicle]" owns children like
    # "[mcp_servers.agent-chronicle.env]" (same for the quoted/old variants).
    child_prefixes = tuple(header[:-1] + "." for header in headers)
    block_line = block if block.endswith("\n") else block + "\n"

    def _is_header(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("[") and stripped.endswith("]")

    def _is_recognized(line: str) -> bool:
        stripped = line.strip()
        return stripped in headers or (stripped.startswith(child_prefixes) and stripped.endswith("]"))

    header_indices = [i for i, line in enumerate(lines) if _is_header(line)]
    spans: list[tuple[int, int]] = []
    for position, index in enumerate(header_indices):
        if not _is_recognized(lines[index]):
            continue
        start = index
        if comment_markers and index > 0 and lines[index - 1].strip() in comment_markers:
            start = index - 1
        end = header_indices[position + 1] if position + 1 < len(header_indices) else len(lines)
        spans.append((start, end))
    if not spans:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        path.write_text(existing + prefix + block_line)
        return "wrote"
    # Merge overlapping/adjacent spans (a recognized child section directly
    # follows its parent) and drop everything they cover.
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    insert_at = merged[0][0]
    removed = {i for start, end in merged for i in range(start, end)}
    new_lines: list[str] = []
    inserted = False
    for index, line in enumerate(lines):
        if index == insert_at:
            new_lines.append(block_line)
            inserted = True
        if index in removed:
            continue
        new_lines.append(line)
    if not inserted:  # pragma: no cover - spans always start within lines
        new_lines.append(block_line)
    path.write_text("".join(new_lines))
    return "updated"


@app.command("init")
def init_project(
    project_dir: Annotated[Path, typer.Option(help="Project directory to initialize.")] = Path("."),
    force: Annotated[bool, typer.Option(help="Overwrite an existing policy file.")] = False,
    agent: Annotated[
        list[str] | None,
        typer.Option(help="Install observe-only instructions for an agent: claude-code, codex, generic, hermes, opencode, or openclaw. Repeatable."),
    ] = None,
    mcp: Annotated[bool, typer.Option("--mcp/--no-mcp", help="Preview MCP setup for requested agents.")] = True,
    write_mcp: Annotated[
        bool,
        typer.Option(help="Write implemented project-local MCP config for requested agents. Default only previews."),
    ] = False,
    mcp_command: Annotated[
        Optional[str],
        typer.Option(help="Command path to write into MCP config. Use when agentacct is not on the agent's PATH."),
    ] = None,
    relative_store_path: Annotated[
        bool,
        typer.Option(
            "--relative-store-path",
            help=(
                "Write the relative '.agent-sentinel/state' store path into MCP config instead of the absolute default. "
                "Use for configs committed and shared across machines; `mcp serve` resolves it against the project root."
            ),
        ),
    ] = False,
) -> dict[str, str]:
    """Initialize observe-only agentacct project tracking."""
    project_dir = project_dir.resolve()
    # Temporary Claude Code worktrees belong to their owning repository: the
    # store resolver, hook capture, and MCP config defaults all remap
    # <owner>/.claude/worktrees/<name> to the owner, so init must not create a
    # vanishing worktree-local store (or write its path into committed MCP
    # config). A worktree with its own PRE-EXISTING store keeps it.
    if not (project_dir / ".agent-sentinel" / "state").is_dir():
        worktree_owner = claude_worktree_owner_dir(project_dir)
        if worktree_owner is not None:
            console.print("Claude Code worktree detected.")
            console.print(f"Initializing the owning project instead of this temporary worktree: {worktree_owner}")
            project_dir = worktree_owner.resolve()
    mcp_command = _resolve_mcp_command(mcp_command)
    requested_agents = agent or []
    unsupported = sorted(set(requested_agents) - set(AGENT_INSTRUCTION_TARGETS))
    if unsupported:
        raise typer.BadParameter(f"unsupported agent target(s): {', '.join(unsupported)}")
    if write_mcp and not requested_agents:
        typer.echo("--write-mcp requires at least one --agent")
        raise typer.Exit(2)
    policy_path = default_policy_path(project_dir)
    policy_created = False
    if policy_path.exists() and not force:
        console.print(f"Policy already exists: {DEFAULT_POLICY_FILE}")
    else:
        write_default_policy(project_dir, force=force)
        policy_created = True
    state_dir = project_dir / ".agent-sentinel" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    gitignore_entries = _append_missing_gitignore_entries(project_dir)
    instruction_paths = [_install_agent_instructions(project_dir, name) for name in requested_agents]

    if policy_created:
        console.print(f"Created policy: {DEFAULT_POLICY_FILE}")
    console.print(f"Project-local state: {state_dir}")
    if gitignore_entries:
        console.print(f"Updated gitignore: {project_dir / '.gitignore'}")
    for instruction_path in instruction_paths:
        console.print(f"Updated agent instructions: {instruction_path}")
    if "claude-code" in requested_agents:
        _print_claude_worktree_store_hint(project_dir, command=mcp_command)
    install_receipts: dict[str, str] = {}
    if requested_agents and mcp:
        # Absolute by default (a relative path is resolved against the MCP
        # client's launch cwd and used to silently create stray stores).
        # --relative-store-path opts back into the portable relative form.
        config_store_dir: Path | str = ".agent-sentinel/state" if relative_store_path else _mcp_store_dir_for_project(project_dir)
        console.print("MCP setup:")
        if mcp_command != "agentacct":
            console.print(f"MCP executable: {mcp_command}")
        for name in requested_agents:
            if name == "claude-code":
                if write_mcp:
                    path, action = _write_claude_mcp_config(project_dir, config_store_dir, command=mcp_command)
                    install_receipts[name] = action
                    if action != "skipped":
                        console.print(f"Wrote Claude Code MCP config: {path}")
                else:
                    _print_claude_mcp_setup(config_store_dir, command=mcp_command)
                    console.print("Preview only. Re-run with --write-mcp to create/update project .mcp.json.")
            elif name == "codex":
                if write_mcp:
                    path, action = _write_codex_mcp_config(project_dir, config_store_dir, command=mcp_command)
                    install_receipts[name] = action
                    if action != "skipped":
                        console.print(f"{action.capitalize()} Codex MCP config block: {path}")
                else:
                    _print_codex_mcp_setup(config_store_dir, command=mcp_command)
                    console.print("Preview only. Re-run with --write-mcp to create/update project .codex/config.toml.")
            else:
                _print_agent_mcp_preview(name, config_store_dir, command=mcp_command)
                if write_mcp:
                    console.print(f"{name}: project-local MCP config write is not available; use the preview command above.")
                else:
                    console.print("Preview only. agentacct will not modify global/profile agent config.")
    console.print("Next checks:")
    console.print(f"- agentacct doctor --project-dir {shlex.quote(str(project_dir))}")
    console.print("- agentacct mcp doctor  (read-only diagnostics)")
    console.print("- agentacct demo --store-dir .agent-sentinel/state")
    console.print("- agentacct serve --store-dir .agent-sentinel/state")
    return install_receipts


def _consent_to_write(prompt: str, *, assume_yes: bool) -> bool:
    """Ask before writing a USER-owned file. --yes skips the prompt; a
    non-interactive run without --yes declines (never touch a user file blindly)."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return typer.confirm(prompt, default=True)


def _onboard_global_claude(store_dir: Path, command: str, *, assume_yes: bool) -> bool:
    """Configure Claude Code at USER scope (zero repo files). Returns readiness."""
    home = Path.home()
    # 1. standing "record your work" instructions -> ~/.claude/CLAUDE.md
    setup_instructions(
        agent="claude-code", user=True, path=None, remove=False, dry_run=False, store_dir=store_dir
    )
    # 2. hook wrapper OUTSIDE the store (~/.claude/hooks/) + user settings example
    #    rendered with the wrapper's ABSOLUTE path and an embedded --store-dir.
    install_claude_code_hook(
        home, force=True, store_dir=store_dir, user_settings_example=True
    )
    _hook_path, example_path = claude_code_hook_paths(home)
    # 3. merge the hook + ENABLE_TOOL_SEARCH env into ~/.claude/settings.json (consent)
    settings_target = home / ".claude" / "settings.json"
    if _consent_to_write(
        f"Merge the agentacct hook + ENABLE_TOOL_SEARCH=auto into {settings_target}?",
        assume_yes=assume_yes,
    ):
        try:
            _merge_claude_settings_from_example(example_path, settings_target)
            console.print(f"Merged Claude Code hook + env into {settings_target}")
        except RuntimeManagerError as exc:
            console.print(f"Left {settings_target} unchanged: {exc}")
            console.print(f"Merge {example_path} into {settings_target} yourself to finish.")
    else:
        console.print(
            f"Skipped {settings_target}. Merge the 'hooks' and 'env' blocks from "
            f"{example_path} into it yourself (needed for recording)."
        )
    # 4. native user-scope MCP registration -> ~/.claude.json (merge, preserve keys)
    path, action = _write_user_claude_mcp_config(home / ".claude.json", store_dir, command=command)
    if action == "skipped":
        return False
    console.print(f"{action.capitalize()} Claude Code MCP server in {path}")
    console.print("Start a NEW Claude Code session so it loads the server + hook.")
    return True


def _onboard_global_codex(store_dir: Path, command: str) -> bool:
    """Configure Codex at USER scope (zero repo files). Returns readiness."""
    home = Path.home()
    setup_instructions(
        agent="codex", user=True, path=None, remove=False, dry_run=False, store_dir=store_dir
    )
    path, action = _write_codex_mcp_config_at(home / ".codex" / "config.toml", store_dir, command=command)
    if action == "skipped":
        return False
    console.print(f"{action.capitalize()} Codex MCP server block in {path}")
    console.print("Start a NEW Codex session so it loads the server.")
    return True


def _print_opencode_global_preview(store_dir: Path, command: str) -> None:
    console.print(
        "OpenCode detected. agentacct does not write OpenCode config; run this to register the server:"
    )
    print(
        f"opencode mcp add agentacct -- {shlex.quote(command)} mcp serve "
        f"--store-dir {shlex.quote(str(store_dir))}"
    )
    console.print(
        "OpenCode gets the 'record your work' instruction from the MCP server on connect (no AGENTS.md needed)."
    )


def _warn_global_store_mismatches(store_dir: Path, command: str) -> None:
    """Warn when a surface agentacct does NOT rewrite still points elsewhere.

    Global onboard writes Claude Code and Codex config, but the OpenCode
    registration and an already-merged Claude hook command are the user's to
    update. Silently leaving one pointed at a different store splits recording
    across two ledgers (the hook writes join context to store A while MCP
    records work in store B), which reads as "attribution mysteriously got
    worse". Detection is best-effort and never fails onboarding.
    """

    target = str(store_dir)

    opencode_config = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    try:
        if opencode_config.is_file():
            text = opencode_config.read_text(encoding="utf-8")
            if "agentacct" in text and target not in text:
                console.print(
                    "OpenCode is registered against a DIFFERENT store than this install. "
                    "agentacct cannot rewrite OpenCode config — re-register it:"
                )
                print(
                    f"opencode mcp remove agentacct; opencode mcp add agentacct -- "
                    f"{shlex.quote(command)} mcp serve --store-dir {shlex.quote(target)}"
                )
    except OSError:
        pass

    settings = Path.home() / ".claude" / "settings.json"
    try:
        if settings.is_file():
            payload = json.loads(settings.read_text(encoding="utf-8"))
            commands = [
                str(entry.get("command") or "")
                for rows in (payload.get("hooks") or {}).values()
                if isinstance(rows, list)
                for row in rows
                if isinstance(row, dict)
                for entry in (row.get("hooks") or [])
                if isinstance(entry, dict)
            ]
            stale = [
                text
                for text in commands
                if CLAUDE_HOOK_RELATIVE_PATH.name in text
                and str(Path.home() / ".claude" / "hooks") not in text
            ]
            if stale:
                console.print(
                    f"Your ~/.claude/settings.json runs a hook wrapper from outside ~/.claude/hooks/ "
                    f"({stale[0].split()[-1]}). It may capture join context into another store — "
                    f"re-point it at the wrapper this install manages:"
                )
                print(
                    f"agentacct hooks claude-code install --project-dir {shlex.quote(str(Path.home()))} "
                    f"--store-dir {shlex.quote(target)} --user-settings-example --force"
                )
    except (OSError, json.JSONDecodeError):
        pass


def _resync_integration(
    store_dir: Path, clients, command: str, *, assume_yes: bool = True
) -> tuple[list[str], list[str]]:
    """Re-run the idempotent onboard writers for already-configured clients so their
    MCP config / hooks / instructions match the installed agentacct version.

    Reuses the exact per-client onboard helpers (which preserve user customizations
    — the settings merge is gated by ``assume_yes``, the MCP write is a
    key-preserving upsert, instructions replace only the managed block, and a user's
    own statusLine is never touched). Best-effort per client: a failure for one
    never aborts the caller (e.g. ``start``).

    Returns ``(resynced, errored)``. A writer that RAISES (a real, possibly
    transient failure — locked/corrupt config, disk, permissions) lands in
    ``errored`` so the caller can leave the version stamp stale and retry next
    start. A writer that merely returns ``False`` (a steady-state SKIP, e.g. a
    user's custom pre-rename block agentacct refuses to touch) is neither resynced
    nor errored, so the stamp still advances and we don't loop forever.
    """

    client_set = {str(c).strip().lower() for c in clients}
    resynced: list[str] = []
    errored: list[str] = []
    if "claude-code" in client_set:
        try:
            if _onboard_global_claude(store_dir, command, assume_yes=assume_yes):
                resynced.append("claude-code")
        except Exception:  # noqa: BLE001 - re-sync must never break the caller.
            errored.append("claude-code")
    if "codex" in client_set:
        try:
            if _onboard_global_codex(store_dir, command):
                resynced.append("codex")
        except Exception:  # noqa: BLE001
            errored.append("codex")
    return resynced, errored


def _resync_integration_if_stale(store_dir: Path, *, force: bool = False) -> dict[str, Any]:
    """Re-sync client integrations when the install version changed (or ``force``).

    Reads the activation record; if its stamped agentacct version differs from the
    installed one (or ``force``), re-runs the idempotent writers for the recorded
    clients and re-stamps. A cheap no-op when already current — safe to call on
    every ``start`` so a client's MCP/instructions/hooks never drift behind an
    agentacct upgrade. Never raises: a re-sync problem must not stop the runtime."""

    try:
        store = ActivationStateStore(store_dir)
        snap = store.snapshot()
    except Exception:  # noqa: BLE001
        return {"status": "unavailable"}
    if not isinstance(snap, Mapping) or snap.get("issue"):
        return {"status": "no-activation"}
    clients = [str(c) for c in (snap.get("clients") or []) if str(c).strip()]
    if not clients:
        return {"status": "no-clients"}
    # Only global installs are re-synced here: `_onboard_global_*` write USER-scope
    # config (~/.claude, ~/.codex), which is correct only when the record is the
    # global one (project_dir == home, as _onboard_global stamps it). Re-running
    # them for a project-scoped install would write the wrong (user) scope, so skip.
    try:
        is_global = str(Path(snap.get("project_dir") or "").expanduser().resolve()) == str(
            Path.home().resolve()
        )
    except Exception:  # noqa: BLE001
        is_global = False
    if not is_global:
        return {"status": "project-scope-skipped"}
    current = _package_version()
    stamped = snap.get("agentacct_version")
    if not force and stamped == current:
        return {"status": "current", "version": current}
    try:
        command = _resolve_absolute_mcp_command()
    except Exception:  # noqa: BLE001
        return {"status": "unavailable"}
    resynced, errored = _resync_integration(store_dir, clients, command, assume_yes=True)
    # Advance the version stamp ONLY when no client's writer raised. A raised
    # writer stays stale so the next start retries it (fixing a transient failure);
    # a client that merely returned False (steady-state skip) is not "errored", so
    # a legitimately-unwritable install still advances and never loops every start.
    if not errored:
        try:
            project_dir = snap.get("project_dir") or str(Path.home())
            store.mark_configured(project_dir=project_dir, clients=clients, agentacct_version=current)
        except Exception:  # noqa: BLE001 - writers ran; a stamp failure just retries next start.
            pass
    return {
        "status": "partial" if errored else "resynced",
        "from": stamped,
        "to": current,
        "clients": resynced,
        "errored": errored,
    }


def _onboard_global(*, agent: str, port: int, start_runtime: bool, mcp: bool, assume_yes: bool) -> None:
    """Install agentacct ONCE, machine-wide: user-scope MCP + hooks + instructions
    against one global store, writing ZERO files into any repo."""
    # An upgrading user's ledger may already live in a recognized global store
    # (e.g. the pre-rename ~/.agent-sentinel-global). Keep using it instead of
    # silently repointing every client at a new, empty store — that strands the
    # history and splits clients across two ledgers.
    store_dir, store_pre_existing = onboard_global_store_dir()
    store_dir.mkdir(parents=True, exist_ok=True)
    # Absolute path: GUI clients do not inherit the shell PATH (see helper).
    command = _resolve_absolute_mcp_command()

    found = {row.client for row in discover_usage_sources() if row.status == "found"}
    if agent in {"auto", "all"}:
        targets = [client for client in ("claude-code", "codex") if client in found] or [
            "claude-code",
            "codex",
        ]
        opencode_detected = "opencode" in found
    elif agent in {"claude-code", "codex"}:
        targets = [agent]
        opencode_detected = False
    elif agent == "opencode":
        targets = []
        opencode_detected = True
    else:
        raise typer.BadParameter(
            "global scope configures claude-code and codex (opencode is previewed). "
            "Use --scope project for other clients."
        )

    console.print("agentacct global install: one machine-wide store, ZERO files written into any repo.")
    console.print(f"- global store: {store_dir}")
    if store_pre_existing:
        console.print("  (existing global store with recorded history — reusing it, not starting a new one)")
    console.print(f"- clients: {', '.join(targets) if targets else '(opencode preview only)'}")
    console.print("- writes user-level MCP + hooks + instructions; merges ~/.claude/settings.json only with your ok")

    recording_clients: list[str] = []
    if mcp and "claude-code" in targets:
        if _onboard_global_claude(store_dir, command, assume_yes=assume_yes):
            recording_clients.append("claude-code")
    if mcp and "codex" in targets:
        if _onboard_global_codex(store_dir, command):
            recording_clients.append("codex")
    if opencode_detected:
        _print_opencode_global_preview(store_dir, command)

    imported = _local_usage_import_payload(
        store_dir=store_dir,
        client="all",
        codex_home=None,
        claude_home=None,
        opencode_home=None,
        hermes_home=None,
        openclaw_home=None,
        cursor_home=None,
        limit_sessions=20,
        dry_run=False,
        estimate_costs=True,
        refresh=True,
    )
    console.print(
        "Initial local usage sync: "
        f"imported={int(imported.get('imported_events', 0) or 0)} "
        f"refreshed={int(imported.get('refreshed_events', 0) or 0)}"
    )

    if recording_clients:
        try:
            ActivationStateStore(store_dir).mark_configured(
                project_dir=Path.home(), clients=recording_clients,
                agentacct_version=_package_version(),
            )
        except ActivationStateError as exc:
            console.print(f"Onboarding stopped safely: {exc}")
            raise typer.Exit(1) from exc

    if start_runtime:
        _health, external_watcher_running = _runtime_ingestion_health(store_dir)
        try:
            runtime_payload = _managed_runtime(store_dir, port=port, project_dir=None).start(
                external_watcher_running=external_watcher_running
            )
        except RuntimeManagerError as exc:
            console.print("Global install completed, but the managed runtime did not start.")
            console.print(f"Cause: {exc}")
            console.print("Next: run `agentacct start`.")
            raise typer.Exit(1) from exc
        console.print(f"Local API: {runtime_payload['dashboard_url']}")

    # Surfaces agentacct does NOT rewrite (OpenCode config, an already-merged
    # hook command) can still point at another store — say so loudly rather than
    # let recording silently split across two ledgers.
    _warn_global_store_mismatches(store_dir, command)

    if recording_clients:
        console.print("Ready. Open a NEW agent session (in ANY repo) — recording is machine-wide now.")
    else:
        console.print("Local usage capture is ready; no semantic recording client was configured.")


@app.command("onboard")
def onboard(
    project_dir: Annotated[Path, typer.Option(help="Project directory to connect (project scope only).")] = Path("."),
    agent: Annotated[
        str,
        typer.Option(help="Client to configure: auto, all, codex, claude-code, hermes, opencode, or openclaw."),
    ] = "auto",
    scope: Annotated[
        str,
        typer.Option(
            help="Install scope: 'global' (default) installs ONCE machine-wide with zero files in any repo; "
            "'project' configures only this repo (legacy per-repo install)."
        ),
    ] = "global",
    port: Annotated[int, typer.Option(help="Managed localhost dashboard port.")] = 8765,
    start_runtime: Annotated[
        bool,
        typer.Option("--start/--no-start", help="Start continuous usage sync and the dashboard."),
    ] = True,
    mcp: Annotated[
        bool,
        typer.Option("--mcp/--no-mcp", help="Write implemented MCP configuration."),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Assume yes for the user-level ~/.claude/settings.json merge (global scope). "
            "Required to merge it in a non-interactive run.",
        ),
    ] = False,
) -> None:
    """Install agentacct once machine-wide (default), or connect a single project.

    Global scope writes ZERO files into your repo: user-level MCP + hooks +
    instructions against one machine-wide store. Use --scope project for the
    legacy per-repo install.
    """

    if scope not in {"global", "project"}:
        raise typer.BadParameter("scope must be 'global' or 'project'")
    if scope == "global":
        _onboard_global(agent=agent, port=port, start_runtime=start_runtime, mcp=mcp, assume_yes=yes)
        return

    project_dir, remapped_worktree = _onboarding_project_dir(project_dir)
    if remapped_worktree is not None:
        console.print("Claude Code worktree detected.")
        console.print(
            "Connecting the owning project so configuration, usage, activation, and runtime share one stable store: "
            f"{project_dir}"
        )
    if agent not in {"auto", "all", *AGENT_INSTRUCTION_TARGETS}:
        raise typer.BadParameter("unsupported --agent; choose auto, all, or a recognized client target")
    discovered = discover_usage_sources()
    found_clients = [
        row.client
        for row in discovered
        if row.status == "found" and row.client in AGENT_INSTRUCTION_TARGETS
    ]
    importable_clients = [
        row.client
        for row in discovered
        if row.status == "found"
        and row.importer is not None
        and row.client in AGENT_INSTRUCTION_TARGETS
    ]
    if agent == "auto":
        requested_agents = list(dict.fromkeys(importable_clients))
    elif agent == "all":
        requested_agents = list(dict.fromkeys(found_clients or ["codex", "claude-code"]))
    else:
        requested_agents = [agent]
    if not requested_agents:
        detected_but_unimportable = [
            row
            for row in discovered
            if row.status == "found" and row.importer is None
        ]
        if detected_but_unimportable:
            console.print(
                "Local agent data was detected, but no importable usage path is available: "
                + ", ".join(row.display_name for row in detected_but_unimportable)
                + "."
            )
            opencode_row = next(
                (row for row in detected_but_unimportable if row.client == "opencode"),
                None,
            )
            if opencode_row is not None and any(
                "multiple OpenCode homes" in note for note in opencode_row.notes
            ):
                console.print(
                    "Multiple OpenCode homes were detected; select one with `--opencode-home` so raw session ids cannot cross namespaces."
                )
            elif opencode_row is not None:
                console.print(
                    "OpenCode was detected but no single importable store resolved; ensure one home holds an opencode.db session store (or exported JSON) and select it with `--opencode-home`."
                )
        else:
            console.print("No readable known local usage source was found.")
            console.print(
                "Install or run a client with an implemented local import path, then retry `agentacct onboard`."
            )
        raise typer.Exit(1)

    store_dir = project_dir / ".agent-sentinel" / "state"
    console.print("agentacct will make project-local changes only:")
    console.print(f"- state: {store_dir}")
    console.print(f"- clients: {', '.join(requested_agents)}")
    console.print("- no provider keys, billing connection, or global client settings")
    install_receipts = init_project(
        project_dir=project_dir,
        force=False,
        agent=requested_agents,
        mcp=mcp,
        write_mcp=mcp,
        mcp_command=None,
        relative_store_path=False,
    ) or {}

    claude_recording_verified = False
    adapter_recovery_steps: list[str] = []
    if "claude-code" in requested_agents:
        hook_path, example_path = claude_code_hook_paths(project_dir)
        if not hook_path.exists() or not example_path.exists():
            claude_code_install(
                project_dir=project_dir,
                dry_run=False,
                force=False,
                store_dir=store_dir,
                user_settings_example=False,
            )
        try:
            settings_path, settings_action = _merge_claude_project_settings(project_dir)
        except RuntimeManagerError as exc:
            console.print(f"Onboarding stopped safely: {exc}")
            raise typer.Exit(1) from exc
        console.print(f"Claude Code settings {settings_action}: {settings_path}")
        hook_checks = claude_code_hook_doctor_checks(project_dir)
        failed_hook_checks = [
            str(check.get("name") or "unknown")
            for check in hook_checks
            if check.get("status") != "ok"
        ]
        if failed_hook_checks:
            console.print(
                "Claude Code recording adapter could not be verified: "
                f"{', '.join(failed_hook_checks)}."
            )
            adapter_recovery_steps.append(
                "run `agentacct hooks claude-code doctor`, review the warnings, then reinstall the hook"
            )
        else:
            claude_recording_verified = True

    # One-command readiness is evidence, not intent. Codex is configured only
    # when its project-local MCP block was written. Claude Code additionally
    # requires the verified hook bridge, but that bridge alone only supplies
    # join context/directives; without MCP it cannot record semantic sections.
    # Other clients currently receive setup instructions/previews only, so
    # they must never be persisted as recording-ready until an adapter-specific
    # install receipt exists.
    recording_clients = []
    if (
        "claude-code" in requested_agents
        and claude_recording_verified
        and mcp
        and install_receipts.get("claude-code") == "wrote"
    ):
        recording_clients.append("claude-code")
    elif "claude-code" in requested_agents and not mcp:
        console.print(
            "Claude Code join hooks were installed, but semantic work recording requires the project MCP server."
        )
        adapter_recovery_steps.append(
            "rerun `agentacct onboard --agent claude-code --mcp`"
        )
    elif "claude-code" in requested_agents and mcp:
        console.print(
            "Claude Code recording was not marked ready because its MCP registration or hook bridge was not verified."
        )
        adapter_recovery_steps.append(
            "resolve the Claude Code MCP/hook warning, then rerun onboarding"
        )
    if (
        "codex" in requested_agents
        and mcp
        and install_receipts.get("codex") in {"wrote", "updated"}
    ):
        recording_clients.append("codex")
    elif "codex" in requested_agents and mcp:
        console.print(
            "Codex recording adapter was not configured because agentacct preserved a conflicting existing MCP registration."
        )
        adapter_recovery_steps.append(
            "review the existing `agent-sentinel` block in `.codex/config.toml`, then rerun onboarding after resolving the conflict"
        )
    manual_recording_clients = [
        client for client in requested_agents if client not in {"codex", "claude-code"}
    ]
    if manual_recording_clients:
        console.print(
            "One-command work recording is not available for: "
            f"{', '.join(manual_recording_clients)}. agentacct installed local instructions and usage capture only; "
            "use the advanced manual setup for that client."
        )
    if recording_clients:
        try:
            ActivationStateStore(store_dir).mark_configured(
                project_dir=project_dir,
                clients=recording_clients,
                agentacct_version=_package_version(),
            )
        except ActivationStateError as exc:
            console.print(f"Onboarding stopped safely: {exc}")
            raise typer.Exit(1) from exc

    imported = _local_usage_import_payload(
        store_dir=store_dir,
        client="all",
        codex_home=None,
        claude_home=None,
        opencode_home=None,
        hermes_home=None,
        openclaw_home=None,
        cursor_home=None,
        limit_sessions=20,
        dry_run=False,
        estimate_costs=True,
        refresh=True,
    )
    console.print(
        "Initial local usage sync: "
        f"imported={int(imported.get('imported_events', 0) or 0)} "
        f"refreshed={int(imported.get('refreshed_events', 0) or 0)}"
    )
    if imported.get("incomplete_alias_migrations"):
        console.print(
            "Initial sync preserved "
            f"{int(imported.get('incomplete_alias_migrations', 0) or 0)} legacy session(s) "
            "because the client log did not reproduce every stored model lane. "
            "Repair that agent's log/source path, then run `agentacct usage import-local --refresh`."
        )

    runtime_payload: dict[str, Any] | None = None
    if start_runtime:
        _health, external_watcher_running = _runtime_ingestion_health(store_dir)
        try:
            runtime_payload = _managed_runtime(
                store_dir,
                port=port,
                project_dir=project_dir,
            ).start(external_watcher_running=external_watcher_running)
        except RuntimeManagerError as exc:
            console.print("Project connection and initial sync completed, but the managed runtime did not start.")
            console.print(f"Cause: {exc}")
            console.print("Next: run `agentacct repair`, then `agentacct start`.")
            raise typer.Exit(1) from exc
        console.print(f"Local API: {runtime_payload['dashboard_url']}")

    if recording_clients:
        console.print("Ready for a real Task.")
        console.print(
            "Required: open a NEW agent session in this project. MCP servers and hooks bind when the client session starts."
        )
        console.print("agentacct will keep usage-only activity honest if semantic work context is not recorded.")
    else:
        console.print("Local usage capture is ready; semantic work recording is not configured.")
        if manual_recording_clients:
            console.print("Next: complete that client's advanced manual recording setup, then open a NEW agent session.")
        elif adapter_recovery_steps:
            for step in adapter_recovery_steps:
                console.print(f"Next: {step}.")
        else:
            console.print("Next: run `agentacct onboard --mcp`, then open a NEW agent session.")


# Bounded re-ensure interval for the foreground managed-runtime supervisor.
# launchd KeepAlive / systemd Restart supervise the foreground process; this
# loop is the managed runtime supervising its own watcher + dashboard.
_FOREGROUND_TICK_SECONDS = 3.0


def _supervise_foreground(
    ensure: Callable[[], Any],
    teardown: Callable[[], Any],
    *,
    interval: float = _FOREGROUND_TICK_SECONDS,
    stop_event: threading.Event | None = None,
    max_ticks: int | None = None,
    install_signal_handlers: bool = True,
    on_tick: Callable[[int], None] | None = None,
) -> int:
    """Block as the managed-runtime supervisor until SIGTERM/SIGINT or stop.

    Each tick idempotently re-ensures the runtime (a died watcher/dashboard is
    respawned by ``ensure``), then waits ``interval`` seconds on ``stop_event``.
    The loop is fully testable: inject ``stop_event`` (pre-set or set from a
    handler), ``max_ticks`` to bound iterations, and ``interval=0`` to avoid a
    real sleep. Returns the number of completed ticks.
    """

    stop = stop_event if stop_event is not None else threading.Event()
    previous_handlers: dict[int, Any] = {}
    if install_signal_handlers:

        def _request_stop(_signum: int, _frame: Any) -> None:
            stop.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, _request_stop)
    ticks = 0
    try:
        while not stop.is_set():
            ensure()
            ticks += 1
            if on_tick is not None:
                on_tick(ticks)
            if max_ticks is not None and ticks >= max_ticks:
                break
            stop.wait(interval)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        teardown()
    return ticks


def _resync_client_integration_on_start(store_dir: Path) -> None:
    """Version-gated integration re-sync invoked from `start` — quiet unless it
    actually refreshed something, and never fatal to the runtime."""

    try:
        result = _resync_integration_if_stale(store_dir)
    except Exception:  # noqa: BLE001
        return
    resynced = result.get("clients") or []
    errored = result.get("errored") or []
    if resynced:
        console.print(
            f"Re-synced client integration for agentacct {result.get('to')} "
            f"(installed version changed): {', '.join(resynced)}"
        )
    if errored:
        console.print(
            f"Could not re-sync {', '.join(errored)} for agentacct {result.get('to')} "
            "— will retry on next start; run `agentacct sync` or check that client's config."
        )


@app.command("sync")
def sync_integration(
    store_dir: Annotated[Optional[Path], typer.Option(help=_DASHBOARD_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Re-sync client integrations (MCP config, hooks, instructions) to the installed
    agentacct version.

    Runs automatically on `start` when the version changes; run it yourself to force
    a refresh (e.g. after an upgrade). Idempotent and non-clobbering: it re-writes
    only agentacct's own config/instructions/hooks and never touches your own
    settings (a custom statusLine is left alone).
    """

    resolved = _resolve_dashboard_cli_store_dir(store_dir).path
    result = _resync_integration_if_stale(resolved, force=True)
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return
    status = result.get("status")
    if status == "no-activation":
        console.print("No activation record found — run `agentacct onboard` first.")
    elif status == "no-clients":
        console.print("No configured clients to re-sync.")
    elif status == "project-scope-skipped":
        console.print("Project-scoped install — re-sync only runs for global installs. Re-run `agentacct onboard` in the project instead.")
    elif status == "unavailable":
        console.print("Could not read the activation record; nothing re-synced.")
    elif status in ("resynced", "partial"):
        clients = ", ".join(result.get("clients") or []) or "(none)"
        console.print(f"Re-synced client integration to agentacct {result.get('to')}: {clients}")
        errored = result.get("errored") or []
        if errored:
            console.print(f"Could not re-sync: {', '.join(errored)} — check that client's config and retry.")
    else:
        console.print(f"Integration re-sync: {status}")


@app.command("start")
def runtime_start(
    store_dir: Annotated[Optional[Path], typer.Option(help=_DASHBOARD_STORE_DIR_HELP)] = None,
    host: Annotated[str, typer.Option(help="Managed dashboard host (localhost only).")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Managed dashboard port.")] = 8765,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    foreground: Annotated[
        bool,
        typer.Option(
            "--foreground",
            help=(
                "Block as the managed-runtime supervisor: idempotently keep the watcher + "
                "dashboard alive until SIGTERM/SIGINT, then stop them cleanly. This is the "
                "process launchd KeepAlive / systemd Restart supervises."
            ),
        ),
    ] = False,
) -> None:
    """Idempotently start continuous local sync and the dashboard."""

    if foreground:
        _runtime_start_foreground(store_dir, host=host, port=port, json_output=json_output)
        return
    resolved = _resolve_dashboard_cli_store_dir(store_dir).path
    _resync_client_integration_on_start(resolved)
    _health, external_watcher_running = _runtime_ingestion_health(resolved)
    try:
        payload = _managed_runtime(resolved, host=host, port=port).start(
            external_watcher_running=external_watcher_running
        )
    except RuntimeManagerError as exc:
        console.print(f"Start failed: {exc}")
        raise typer.Exit(1) from exc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        console.print(f"agentacct runtime: {payload['state']}")
        console.print(f"Local API: {payload['dashboard_url']}")


def _runtime_start_foreground(
    store_dir: Path | str | None,
    *,
    host: str,
    port: int,
    json_output: bool,
) -> None:
    """Ensure the runtime is up, then supervise it in the foreground."""

    resolved = _resolve_dashboard_cli_store_dir(store_dir).path
    _resync_client_integration_on_start(resolved)
    manager = _managed_runtime(resolved, host=host, port=port)

    def _ensure() -> dict[str, Any]:
        # Re-resolve watcher health each tick so an externally-adopted or
        # died watcher is reconsidered on every re-ensure.
        _health, external_watcher_running = _runtime_ingestion_health(resolved)
        return manager.start(external_watcher_running=external_watcher_running)

    try:
        payload = _ensure()
    except RuntimeManagerError as exc:
        console.print(f"Start failed: {exc}")
        raise typer.Exit(1) from exc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        console.print(f"agentacct runtime: {payload['state']}")
        console.print(f"Local API: {payload['dashboard_url']}")
        console.print("Supervising in the foreground; send SIGTERM or press Ctrl-C to stop.")

    try:
        _supervise_foreground(_ensure, manager.stop)
    except RuntimeManagerError as exc:
        console.print(f"Shutdown reported: {exc}")
        raise typer.Exit(1) from exc
    if not json_output:
        console.print("Managed agentacct runtime stopped.")


@app.command("status")
def runtime_status(
    store_dir: Annotated[Optional[Path], typer.Option(help=_DASHBOARD_STORE_DIR_HELP)] = None,
    host: Annotated[str, typer.Option(help="Managed dashboard host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Managed dashboard port.")] = 8765,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Show managed process, dashboard, and sync readiness."""

    resolved = _resolve_dashboard_cli_store_dir(store_dir).path
    health, external_watcher_running = _runtime_ingestion_health(resolved)
    payload = _managed_runtime(resolved, host=host, port=port).status(
        external_watcher_running=external_watcher_running
    )
    payload["ingestion_health"] = health
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    table = Table(title="agentacct runtime")
    table.add_column("Component")
    table.add_column("State")
    table.add_column("Details")
    table.add_row("overall", str(payload["state"]), str(payload["store_dir"]))
    table.add_row("local api", str(payload["dashboard_health"]), str(payload["dashboard_url"]))
    table.add_row("continuous sync", str(payload["watcher"]), str(health.get("state") or "unknown"))
    console.print(table)
    for issue in payload.get("issues", []):
        console.print(f"- {issue}")


@app.command("stop")
def runtime_stop(
    store_dir: Annotated[Optional[Path], typer.Option(help=_DASHBOARD_STORE_DIR_HELP)] = None,
    host: Annotated[str, typer.Option(help="Managed dashboard host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Managed dashboard port.")] = 8765,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Stop only processes whose complete agentacct ownership proof matches."""

    resolved = _resolve_dashboard_cli_store_dir(store_dir).path
    try:
        payload = _managed_runtime(resolved, host=host, port=port).stop()
    except RuntimeManagerError as exc:
        console.print(f"Stop refused: {exc}")
        raise typer.Exit(1) from exc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        console.print("Managed agentacct runtime stopped.")


@app.command("repair")
def runtime_repair(
    store_dir: Annotated[Optional[Path], typer.Option(help=_DASHBOARD_STORE_DIR_HELP)] = None,
    host: Annotated[str, typer.Option(help="Managed dashboard host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Managed dashboard port.")] = 8765,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Archive corrupt state or clear dead owned leases without killing unknown processes."""

    resolved = _resolve_dashboard_cli_store_dir(store_dir).path
    try:
        payload = _managed_runtime(resolved, host=host, port=port).repair()
    except RuntimeManagerError as exc:
        console.print(f"Repair stopped safely: {exc}")
        raise typer.Exit(1) from exc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        console.print(f"Repair: {payload.get('action')} (runtime={payload.get('state')})")


@app.command("install-autostart")
def runtime_install_autostart(
    store_dir: Annotated[Optional[Path], typer.Option(help=_DASHBOARD_STORE_DIR_HELP)] = None,
    host: Annotated[str, typer.Option(help="Managed dashboard host (localhost only).")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Managed dashboard port.")] = 8765,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the managed file path, its full content, and the loader command; write and load nothing."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Reserved for parity; the managed unit is fully owned, so install always overwrites it idempotently."),
    ] = False,
) -> None:
    """Install the OS launcher that keeps `agentacct start --foreground` alive (macOS launchd / Linux systemd user unit)."""

    _exit_if_unsupported_platform(sys.platform)
    resolved = _resolve_dashboard_cli_store_dir(store_dir).path
    executable = _managed_runtime(resolved, host=host, port=port).executable
    try:
        plan = autostart_mod.plan_install(
            platform=sys.platform,
            home=Path.home(),
            uid=os.getuid(),
            executable=executable,
            store_dir=str(resolved),
            host=host,
            port=port,
        )
    except AutostartError as exc:
        console.print(f"install-autostart unsupported: {exc}")
        raise typer.Exit(2) from exc

    if dry_run:
        console.print(f"Managed autostart file ({plan.platform}): {plan.path}")
        console.print("--- generated content ---")
        console.print(plan.content)
        console.print("--- loader command ---")
        for command in plan.load_commands:
            console.print(shlex.join(command))
        console.print("Dry run: nothing was written or loaded.")
        return

    try:
        result = autostart_mod.install_autostart(plan)
    except AutostartError as exc:
        console.print(f"install-autostart: {exc}")
        raise typer.Exit(1) from exc
    console.print(f"Installed managed autostart: {result.path}")
    console.print(f"Check status: {shlex.join(plan.check_command)}")
    console.print("Uninstall: agentacct uninstall-autostart")


@app.command("uninstall-autostart")
def runtime_uninstall_autostart(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print what would be unloaded and removed; change nothing."),
    ] = False,
) -> None:
    """Unload and remove the managed autostart file. Idempotent (no error if already absent)."""

    _exit_if_unsupported_platform(sys.platform)
    try:
        plan = autostart_mod.plan_uninstall(
            platform=sys.platform,
            home=Path.home(),
            uid=os.getuid(),
        )
    except AutostartError as exc:
        console.print(f"uninstall-autostart unsupported: {exc}")
        raise typer.Exit(2) from exc

    if dry_run:
        console.print(f"Managed autostart file ({plan.platform}): {plan.path}")
        if plan.path.exists():
            console.print("--- unloader command ---")
            console.print(shlex.join(plan.unload_commands[0]))
            console.print("Dry run: the file would be unloaded and removed.")
        else:
            console.print("Dry run: no managed autostart file present; nothing to do.")
        return

    result = autostart_mod.uninstall_autostart(plan)
    if result.removed_file:
        console.print(f"Removed managed autostart: {result.path}")
    else:
        console.print(f"No managed autostart file present: {result.path}")


@policy_app.command("validate")
def policy_validate(
    project_dir: Annotated[Path, typer.Option(help="Project directory containing .agent-sentinel/policy.yaml.")] = Path("."),
) -> None:
    """Validate the project policy file."""
    path = default_policy_path(project_dir)
    if not path.exists():
        console.print(f"Policy file not found: {path}")
        raise typer.Exit(1)
    try:
        _, errors = load_and_validate_policy(path)
    except Exception as exc:
        console.print(f"Policy invalid: {exc}")
        raise typer.Exit(1) from exc
    if errors:
        for error in errors:
            console.print(error)
        raise typer.Exit(1)
    console.print(f"Policy valid: {path}")


@app.command("doctor")
def doctor(
    project_dir: Annotated[Path, typer.Option(help="Project directory to inspect.")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Check local agentacct project readiness without printing secrets."""
    project_dir = project_dir.resolve()
    policy_path = default_policy_path(project_dir)
    checks: list[tuple[str, bool, str]] = []

    if policy_path.exists():
        try:
            _, errors = load_and_validate_policy(policy_path)
        except Exception:
            errors = ["policy file could not be parsed or loaded; run agentacct policy validate for details"]
        checks.append(("policy file", not errors, f"found at {policy_path}" if not errors else "; ".join(errors)))
    else:
        checks.append(("policy file", False, f"missing at {policy_path}"))

    checks.append(("git repository", (project_dir / ".git").exists(), "detected" if (project_dir / ".git").exists() else "not detected"))
    secrets_ignored = _is_ignored_by_git(project_dir, ".env.local") and _is_ignored_by_git(project_dir, ".env")
    checks.append(("secret files ignored", secrets_ignored, ".env and .env.local protected" if secrets_ignored else "add .env and .env.local to .gitignore"))
    has_openrouter = bool(read_env_alias("AGENTACCT_OPENROUTER_API_KEY"))
    checks.append(("OpenRouter key", has_openrouter, "present" if has_openrouter else "not set; forwarding examples will be dry-run/local only"))

    next_steps: list[str] = []
    if not policy_path.exists():
        next_steps.append(f"Initialize project-local config: agentacct init --project-dir {shlex.quote(str(project_dir))}")
    elif any(name == "policy file" and not ok for name, ok, _details in checks):
        next_steps.append(f"Inspect policy errors, then run: agentacct policy validate --project-dir {shlex.quote(str(project_dir))}")
    if not (project_dir / ".git").exists():
        next_steps.append("Run doctor from a git repository root if you want project-local onboarding checks.")
    if not secrets_ignored:
        next_steps.append("Protect local secrets: add .env and .env.local to .gitignore, or re-run agentacct init.")
    if not has_openrouter:
        next_steps.append(
            "Optional: AGENTACCT_OPENROUTER_API_KEY is only needed for opt-in provider forwarding; "
            "the default observe-only workflow requires no API key."
        )
    next_steps.append("Try a safe local run: agentacct run -- python --version")
    next_steps.append("Open the local dashboard: agentacct serve")

    if json_output:
        payload = {
            "project_dir": str(project_dir),
            "ready": all(ok for name, ok, _details in checks if name != "OpenRouter key"),
            "checks": [
                {"name": name, "status": "ok" if ok else "warn", "ok": ok, "details": details}
                for name, ok, details in checks
            ],
            "next_steps": next_steps,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    table = Table(title="agentacct doctor")
    table.add_column("Status")
    table.add_column("Check")
    table.add_column("Details")
    for name, ok, details in checks:
        table.add_row("ok" if ok else "warn", name, details)
    console.print(table)
    console.print("Next steps:")
    for step in next_steps:
        console.print(f"- {step}")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    max_runtime: Annotated[Optional[str], typer.Option(help="Max runtime before action, e.g. 30s, 10m, 2h.")] = None,
    on_timeout: Annotated[str, typer.Option(help="pause or kill")] = "pause",
    repeated_error_threshold: Annotated[Optional[int], typer.Option(help="Pause/kill when same stderr line repeats N times.")] = None,
    on_repeated_error: Annotated[str, typer.Option(help="pause or kill")] = "pause",
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
) -> None:
    """Run a command under agentacct control. Everything after -- is the child command."""
    command = list(ctx.args)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise typer.BadParameter("provide a command after --")
    if on_timeout not in {"pause", "kill"} or on_repeated_error not in {"pause", "kill"}:
        raise typer.BadParameter("actions must be 'pause' or 'kill'")
    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    result = start_guarded_run(
        command,
        RunOptions(
            store_dir=resolved_store_dir,
            max_runtime_seconds=_parse_duration(max_runtime),
            on_timeout=on_timeout,  # type: ignore[arg-type]
            repeated_error_threshold=repeated_error_threshold,
            on_repeated_error=on_repeated_error,  # type: ignore[arg-type]
        ),
    )
    color = "green" if result.status.value == "completed" else "yellow"
    console.print(f"[{color}]Run {result.run_id}: {result.status.value}[/{color}] — {result.reason}")
    console.print(f"Report: {_display_path(result.run_dir / 'report.md')}")


def _emit_agent_smoke_summary(summary, *, json_output: bool) -> None:
    payload = summary.to_dict()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    table = Table(title=f"Live agent smoke: {payload['agent']}")
    table.add_column("Check")
    table.add_column("Value")
    table.add_row("status", str(payload["status"]))
    table.add_row("exit_code", str(payload["exit_code"]))
    table.add_row("marker_found", str(payload["marker_found"]))
    table.add_row("metadata_ok", str(payload["metadata_ok"]))
    table.add_row("run_dir", str(payload["run_dir"]))
    table.add_row("work_dir", str(payload["work_dir"]))
    console.print(table)


def _run_agent_smoke_command(agent: LiveAgent, *, store_dir: Path | None, work_dir: Path | None, json_output: bool) -> None:
    try:
        summary = run_live_agent_smoke(agent, store_dir=store_dir, work_dir=work_dir)
        assert_live_agent_smoke_passed(summary)
    except AgentSmokeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _emit_agent_smoke_summary(summary, json_output=json_output)


@smoke_app.command("claude-code")
def smoke_claude_code(
    store_dir: Annotated[Optional[Path], typer.Option(help="State directory for smoke artifacts. Defaults to a temporary directory.")] = None,
    work_dir: Annotated[Optional[Path], typer.Option(help="Isolated work directory. Defaults to a temporary directory.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Run a tiny real Claude Code smoke test through `agentacct run`."""
    _run_agent_smoke_command(LiveAgent.CLAUDE_CODE, store_dir=store_dir, work_dir=work_dir, json_output=json_output)


@smoke_app.command("codex")
def smoke_codex(
    store_dir: Annotated[Optional[Path], typer.Option(help="State directory for smoke artifacts. Defaults to a temporary directory.")] = None,
    work_dir: Annotated[Optional[Path], typer.Option(help="Isolated work directory. Defaults to a temporary directory.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Run a tiny real Codex smoke test through `agentacct run`."""
    _run_agent_smoke_command(LiveAgent.CODEX, store_dir=store_dir, work_dir=work_dir, json_output=json_output)


@smoke_app.command("all")
def smoke_all(
    store_dir: Annotated[Optional[Path], typer.Option(help="State directory for smoke artifacts. Defaults to a temporary directory per agent.")] = None,
    work_dir: Annotated[Optional[Path], typer.Option(help="Isolated work directory. Defaults to a temporary directory per agent.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Run tiny real Claude Code and Codex smoke tests through agentacct."""
    summaries = []
    for agent in (LiveAgent.CLAUDE_CODE, LiveAgent.CODEX):
        try:
            summary = run_live_agent_smoke(agent, store_dir=store_dir, work_dir=work_dir)
            assert_live_agent_smoke_passed(summary)
        except AgentSmokeError as exc:
            raise typer.BadParameter(f"{agent.value}: {exc}") from exc
        summaries.append(summary)
    if json_output:
        print(json.dumps([summary.to_dict() for summary in summaries], indent=2, sort_keys=True))
        return
    for summary in summaries:
        _emit_agent_smoke_summary(summary, json_output=False)


@smoke_app.command("mcp-client")
def smoke_mcp_client(
    client: Annotated[str, typer.Option(help="Client to test: hermes, opencode, openclaw, or all.")] = "all",
    provider: Annotated[str, typer.Option(help="Provider: deepseek (default) or openai (opencode only).")] = "deepseek",
    model: Annotated[Optional[str], typer.Option(help="Override the model id, e.g. openai/gpt-5.4-mini-fast.")] = None,
    store_dir: Annotated[Optional[Path], typer.Option(help="State directory for smoke artifacts. Defaults to a temporary directory per client.")] = None,
    deepseek_key_env: Annotated[str, typer.Option(help="Environment variable containing the DeepSeek API key.")] = "DEEPSEEK_API_KEY",
    reuse_opencode_auth: Annotated[
        bool,
        typer.Option(
            "--reuse-opencode-auth",
            help="Let opencode authenticate with its OWN stored auth (~/.local/share/opencode/auth.json) "
            "instead of an env API key. Needed for OAuth providers that have no plain API key.",
        ),
    ] = False,
    acknowledge_real_api: Annotated[bool, typer.Option("--i-understand-this-uses-real-api", help="Required acknowledgement: this command can spend real provider credits.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Run real provider-backed MCP client smokes for Hermes/OpenCode/OpenClaw."""
    if provider not in {"deepseek", "openai"}:
        raise typer.BadParameter("provider must be deepseek or openai")
    if client == "all":
        clients = ["opencode"] if provider != "deepseek" else ["hermes", "opencode", "openclaw"]
    else:
        clients = [client]
    key_env = deepseek_key_env if provider == "deepseek" else None
    results = []
    for item in clients:
        if item not in {"hermes", "opencode", "openclaw"}:
            raise typer.BadParameter("client must be hermes, opencode, openclaw, or all")
        try:
            result = run_mcp_client_smoke(
                item,  # type: ignore[arg-type]
                provider=provider,
                model=model,
                key_env=key_env,
                reuse_client_auth=reuse_opencode_auth and item == "opencode",
                store_dir=store_dir,
                acknowledge_real_api=acknowledge_real_api,
            )
            assert_mcp_client_smoke_passed(result)
        except MCPClientSmokeError as exc:
            raise typer.BadParameter(f"{item}: {exc}") from exc
        results.append(result)
    if json_output:
        print(json.dumps([result.to_dict() for result in results], indent=2, sort_keys=True))
        return
    for result in results:
        table = Table(title=f"MCP client smoke: {result.client}")
        table.add_column("Check")
        table.add_column("Value")
        table.add_row("status", result.status)
        table.add_row("provider", result.provider)
        table.add_row("model", result.model)
        table.add_row("marker_found", str(result.marker_found))
        table.add_row("event_count", str(result.event_count))
        table.add_row("token_cost_observed", str(result.token_cost_observed))
        table.add_row("store_dir", result.store_dir)
        console.print(table)


def _format_cost_summary(ledger: CostLedger, run_id: str) -> str:
    events = ledger.read_events(run_id=run_id)
    total = ledger.total_estimated_cost_usd(run_id=run_id)
    actual_total = ledger.total_actual_provider_cost_usd(run_id=run_id)
    billable_total = ledger.total_billable_cost_usd(run_id=run_id)
    provider_totals: dict[str, float] = {}
    model_totals: dict[str, float] = {}
    confidence_counts: dict[str, int] = {}
    cost_confidence_counts: dict[str, int] = {}
    for event in events:
        cost = float(event.get("estimated_cost_usd") or 0.0)
        provider = str(event.get("provider") or "unknown")
        model = str(event.get("model") or "unknown")
        confidence = str(event.get("usage_confidence") or "unknown")
        cost_confidence = str(event.get("cost_confidence") or "unknown")
        provider_totals[provider] = provider_totals.get(provider, 0.0) + cost
        model_totals[model] = model_totals.get(model, 0.0) + cost
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1
        cost_confidence_counts[cost_confidence] = cost_confidence_counts.get(cost_confidence, 0) + 1

    lines = [
        "## Cost Summary",
        "",
        f"- Estimated total cost: ${total:.6f}",
        f"- Actual provider cost: ${actual_total:.6f}",
        f"- Billable cutoff total: ${billable_total:.6f}",
        f"- Cost events: {len(events)}",
    ]
    if provider_totals:
        lines.extend(["", "### By provider"])
        for provider, cost in sorted(provider_totals.items()):
            lines.append(f"- {provider}: ${cost:.6f}")
    if model_totals:
        lines.extend(["", "### By model"])
        for model, cost in sorted(model_totals.items()):
            lines.append(f"- {model}: ${cost:.6f}")
    if confidence_counts:
        lines.extend(["", "### Usage confidence"])
        for confidence, count in sorted(confidence_counts.items()):
            lines.append(f"- {confidence}: {count}")
    if cost_confidence_counts:
        lines.extend(["", "### Cost confidence"])
        for confidence, count in sorted(cost_confidence_counts.items()):
            lines.append(f"- {confidence}: {count}")
    lines.append("")
    return "\n".join(lines)


DEMO_TASK_SOURCE = """
import pathlib
import sys
import time

output_path = pathlib.Path(sys.argv[1])
print("agentacct demo task: starting", flush=True)
print("step 1/3: inspect task context", flush=True)
time.sleep(0.05)
print("step 2/3: produce a tiny deliverable", flush=True)
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text("agent-chronicle-demo-deliverable\\n", encoding="utf-8")
time.sleep(0.05)
print("demo note: stderr is captured separately", file=sys.stderr, flush=True)
print("step 3/3: verify and summarize", flush=True)
time.sleep(0.05)
print(f"agentacct demo task: wrote {output_path}", flush=True)
print("agentacct demo task: completed", flush=True)
""".strip()


def _demo_check_exit_code(deliverable_path: Path) -> int:
    try:
        return 0 if deliverable_path.read_text(encoding="utf-8") == "agent-chronicle-demo-deliverable\n" else 1
    except FileNotFoundError:
        return 1


@app.command("demo")
def demo(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help="State directory for demo artifacts. Defaults to a THROWAWAY temporary store; pass --store-dir (e.g. .agent-sentinel/state after `agentacct init`) to keep demo runs in a persistent store."),
    ] = None,
    budget_usd: Annotated[float, typer.Option(help="Demo budget used for advisory value scoring. No paid API is called.")] = 0.01,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON summary.")] = False,
) -> None:
    """Run a safe local demo that creates a report, evidence, and value score without provider keys."""
    if not math.isfinite(budget_usd) or budget_usd <= 0:
        raise typer.BadParameter("--budget-usd must be a finite value > 0")
    resolved_store_dir, store_is_temporary = _resolve_scratch_store_dir(store_dir, label="demo")
    store = RunStore(resolved_store_dir)
    demo_id = "demo_" + uuid.uuid4().hex[:8]
    deliverable_path = store.root / "demo-artifacts" / demo_id / "deliverable.txt"
    before_exit_code = _demo_check_exit_code(deliverable_path)
    result = start_guarded_run(
        [sys.executable, "-c", DEMO_TASK_SOURCE, str(deliverable_path)],
        RunOptions(store_dir=resolved_store_dir, poll_interval=0.02),
    )
    after_exit_code = _demo_check_exit_code(deliverable_path)
    report_payload = build_run_report_payload(store, result.run_id)
    outcome = build_machine_check_outcome(
        existing=report_payload["outcome"],
        name="demo-deliverable-file",
        before_exit_code=before_exit_code,
        after_exit_code=after_exit_code,
        before_summary=f"deliverable file did not exist before the run: {deliverable_path}",
        after_summary=f"deliverable file exists with expected content after the run: {deliverable_path}",
    )
    outcome = apply_judge_result(
        outcome,
        {
            "deliverable_score": 88,
            "confidence": "high",
            "reason": "Local demo run completed, produced logs, and recorded objective evidence without provider API calls.",
            "risks": ["Synthetic local demo score; use a real review for production work."],
        },
        source="local_demo",
        model="deterministic-local-demo",
        cost_event_id=None,
    )
    write_outcome(store, result.run_id, outcome)
    value = compute_advisory_value_score(build_run_report_payload(store, result.run_id), budget_usd=budget_usd)
    write_outcome(store, result.run_id, apply_value_score(read_outcome(store, result.run_id) or outcome, value))
    payload = build_run_report_payload(store, result.run_id)
    summary = {
        "run_id": result.run_id,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "store_dir": str(store.root),
        "store_is_temporary": store_is_temporary,
        "report_md": payload["artifacts"]["report_md"],
        "stdout_log": payload["artifacts"]["stdout_log"],
        "stderr_log": payload["artifacts"]["stderr_log"],
        "deliverable_path": str(deliverable_path),
        "value": payload["outcome"]["value"],
        "dashboard_command": f"agentacct serve --store-dir {shlex.quote(str(store.root))}",
        "dashboard_url": "http://127.0.0.1:8765",
        "report_json_command": f"agentacct report {shlex.quote(result.run_id)} --store-dir {shlex.quote(str(store.root))} --json",
        "report_command": f"agentacct report {shlex.quote(result.run_id)} --store-dir {shlex.quote(str(store.root))}",
    }
    if json_output:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    print(f"Demo run {result.run_id}: {result.status.value}")
    if store_is_temporary:
        print(f"Demo store is temporary and will vanish on cleanup/reboot: {store.root}")
        print("Run `agentacct init` and pass --store-dir .agent-sentinel/state to keep demo runs in a persistent project store.")
    print(f"Report: {summary['report_md']}")
    print(f"Demo value score: {summary['value'].get('score')} rating={summary['value'].get('rating')} (synthetic local_demo score for walkthrough)")
    print("This was a demo run: no provider API keys were used, and no paid API calls were made.")
    print("Next: start the local JSON API (or run `agentacct tui`) with:")
    print(f"  {summary['dashboard_command']}")
    print(f"Local API (for scripts and native shells): {summary['dashboard_url']}")
    print("Inspect the run evidence with:")
    print(f"  {summary['report_json_command']}")
    print("Markdown report:")
    print(f"  {summary['report_command']}")


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="Run ID to report, or 'latest'.")] = "latest",
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON for sidecar/MCP consumers.")] = False,
) -> None:
    with _friendly_run_lookup_errors(run_id):
        store = RunStore(_resolve_cli_store_dir(store_dir).path, create=False)
        if run_id == "latest":
            run_id = store.latest_run_id()
        if json_output:
            payload = build_run_report_payload(store, run_id)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return
        path = store.run_dir(run_id) / "report.md"
        if not path.exists():
            # Same friendly "unknown run_id" failure as the --json path when
            # the run does not exist at all.
            store.read_metadata(run_id)
            raise FileNotFoundError(f"run {run_id} has no report.md in this store")
        report_text = path.read_text(encoding="utf-8")
        if "## Cost Summary" not in report_text:
            report_text = report_text.rstrip() + "\n\n" + _format_cost_summary(CostLedger(store.root), run_id)
        console.print(report_text)


@app.command("runs")
def runs_list(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    limit: Annotated[int, typer.Option(help="Number of recent runs to list.")] = 20,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """List recent runs recorded in this store."""
    if limit < 1 or limit > 100:
        raise typer.BadParameter("--limit must be between 1 and 100")
    service = SentinelService(_resolve_cli_store_dir(store_dir).path, create=False)
    runs = service.list_runs(limit=limit)
    if json_output:
        print(json.dumps({"runs": runs}, indent=2, sort_keys=True))
        return
    if not runs:
        print(_NO_RUNS_MESSAGE)
        return
    for run in runs:
        parts = [str(run.get("run_id") or ""), f"status={run.get('status') or ''}"]
        # Runner-recorded runs carry only started_at_monotonic (no wall-clock
        # started_at), so omit the field when absent instead of printing a dead
        # empty `started=` column.
        started = run.get("started_at")
        if started:
            parts.append(f"started={started}")
        command = run.get("command")
        if command:
            command_text = shlex.join(str(part) for part in command) if isinstance(command, list) else str(command)
            parts.append(f"command={command_text}")
        print(" ".join(parts))


@outcome_app.command("record-machine-check")
def outcome_record_machine_check(
    run_id: Annotated[str, typer.Argument(help="Run ID to update, or 'latest'.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    name: Annotated[str, typer.Option(help="Machine check name, e.g. pytest, build, lint.")] = "check",
    before_exit_code: Annotated[Optional[int], typer.Option(help="Exit code before the agent run. 0 means passed; non-zero means failed.")] = None,
    after_exit_code: Annotated[Optional[int], typer.Option(help="Exit code after the agent run. 0 means passed; non-zero means failed.")] = None,
    before_summary: Annotated[Optional[str], typer.Option(help="Short before-state summary.")] = None,
    after_summary: Annotated[Optional[str], typer.Option(help="Short after-state summary.")] = None,
) -> None:
    """Record objective before/after machine-check evidence for a run."""
    with _friendly_run_lookup_errors(run_id):
        store = RunStore(_resolve_cli_store_dir(store_dir).path)
        if run_id == "latest":
            run_id = store.latest_run_id()
        report_payload = build_run_report_payload(store, run_id)
        existing = read_outcome(store, run_id) or report_payload["outcome"]
        outcome = build_machine_check_outcome(
            existing=existing,
            name=name,
            before_exit_code=before_exit_code,
            after_exit_code=after_exit_code,
            before_summary=before_summary,
            after_summary=after_summary,
        )
        write_outcome(store, run_id, outcome)
        checks = outcome["machine_checks"]
        console.print(
            f"Recorded machine check for {run_id}: {name} "
            f"resolved={checks.get('resolved_failures')} introduced={checks.get('introduced_failures')}"
        )


@judge_app.command("prepare")
def judge_prepare(
    run_id: Annotated[str, typer.Argument(help="Run ID to package, or 'latest'.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    task_goal: Annotated[str, typer.Option(help="What the agent was trying to accomplish.")] = "Evaluate this run's deliverable quality.",
    rubric: Annotated[str, typer.Option(help="How the judge should score the deliverable.")] = "Score whether the run produced a useful, relevant, low-risk deliverable for the task goal.",
    output: Annotated[Optional[Path], typer.Option(help="Optional path to write judge_package.json. Defaults to the run directory.")] = None,
) -> None:
    """Prepare an isolated judge package without calling any LLM API."""
    with _friendly_run_lookup_errors(run_id):
        store = RunStore(_resolve_cli_store_dir(store_dir).path, create=False)
        if run_id == "latest":
            run_id = store.latest_run_id()
        report_payload = build_run_report_payload(store, run_id)
        package = build_judge_package(report=report_payload, task_goal=task_goal, rubric=rubric)
        output_path = output or (store.run_dir(run_id) / "judge_package.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(package, indent=2, sort_keys=True), encoding="utf-8")
        console.print(f"Wrote judge package: {output_path}")


@judge_app.command("run")
def judge_run(
    run_id: Annotated[str, typer.Argument(help="Run ID to judge, or 'latest'.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    task_goal: Annotated[str, typer.Option(help="What the agent was trying to accomplish.")] = "Evaluate this run's deliverable quality.",
    rubric: Annotated[str, typer.Option(help="How the judge should score the deliverable.")] = "Score whether the run produced a useful, relevant, low-risk deliverable for the task goal.",
    provider: Annotated[str, typer.Option(help="Judge provider. v0 supports openrouter only.")] = "openrouter",
    model: Annotated[str, typer.Option(help="OpenRouter judge model.")] = "openai/gpt-4o-mini",
    max_total_usd: Annotated[float, typer.Option(help="Hard budget for the judge request.")] = 0.01,
    max_tokens: Annotated[int, typer.Option(help="Max judge output tokens.")] = 256,
) -> None:
    """Run an opt-in LLM judge and write its deliverable score into outcome.json."""
    if provider != "openrouter":
        raise typer.BadParameter("v0 judge run supports --provider openrouter only")
    if max_total_usd <= 0:
        raise typer.BadParameter("--max-total-usd must be > 0")
    if max_tokens <= 0:
        raise typer.BadParameter("--max-tokens must be > 0")
    api_key = read_env_alias("AGENTACCT_OPENROUTER_API_KEY")
    if not api_key:
        raise typer.BadParameter("judge run requires AGENTACCT_OPENROUTER_API_KEY")
    with _friendly_run_lookup_errors(run_id):
        store = RunStore(_resolve_cli_store_dir(store_dir).path, create=False)
        if run_id == "latest":
            run_id = store.latest_run_id()
        report_payload = build_run_report_payload(store, run_id)
        package = build_judge_package(report=report_payload, task_goal=task_goal, rubric=rubric)
        package_path = store.run_dir(run_id) / "judge_package.json"
        package_path.write_text(json.dumps(package, indent=2, sort_keys=True), encoding="utf-8")
        result, event, _ = run_openrouter_judge(
            package=package,
            api_key=api_key,
            model=model,
            ledger=CostLedger(store.root),
            max_total_usd=max_total_usd,
            max_tokens=max_tokens,
        )
        existing = read_outcome(store, run_id) or report_payload["outcome"]
        outcome = apply_judge_result(existing, result, source="openrouter", model=model, cost_event_id=event.get("event_id"))
        write_outcome(store, run_id, outcome)
        console.print(
            f"Judge score for {run_id}: {result['deliverable_score']} "
            f"confidence={result['confidence']} model={model}"
        )


@value_app.command("compute")
def value_compute(
    run_id: Annotated[str, typer.Argument(help="Run ID to score, or 'latest'.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    budget_usd: Annotated[Optional[float], typer.Option(help="Optional budget for cost-pressure adjustment.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the value object as JSON.")] = False,
) -> None:
    """Compute a transparent advisory value score from judge, cost, and machine checks."""
    with _friendly_run_lookup_errors(run_id):
        store = RunStore(_resolve_cli_store_dir(store_dir).path, create=False)
        if run_id == "latest":
            run_id = store.latest_run_id()
        report_payload = build_run_report_payload(store, run_id)
        value = compute_advisory_value_score(report_payload, budget_usd=budget_usd)
        existing = read_outcome(store, run_id) or report_payload["outcome"]
        outcome = apply_value_score(existing, value)
        write_outcome(store, run_id, outcome)
        if json_output:
            print(json.dumps(value, indent=2, sort_keys=True))
        else:
            console.print(f"Value score for {run_id}: {value.get('score')} rating={value.get('rating')} confidence={value.get('confidence')}")
            console.print(str(value.get("reason")))


@app.command()
def pause(run_id: str, store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None) -> None:
    with _friendly_run_lookup_errors(run_id):
        store = RunStore(_resolve_cli_store_dir(store_dir).path, create=False)
        metadata = store.assert_owned(run_id)
        os.killpg(int(metadata["process_group_id"]), signal.SIGSTOP)
        console.print(f"Paused {run_id}")


@app.command()
def resume(run_id: str, store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None) -> None:
    with _friendly_run_lookup_errors(run_id):
        store = RunStore(_resolve_cli_store_dir(store_dir).path, create=False)
        metadata = store.assert_owned(run_id)
        os.killpg(int(metadata["process_group_id"]), signal.SIGCONT)
        console.print(f"Resumed {run_id}")


@app.command(name="kill")
def kill_run(run_id: str, store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None) -> None:
    with _friendly_run_lookup_errors(run_id):
        store = RunStore(_resolve_cli_store_dir(store_dir).path, create=False)
        metadata = store.assert_owned(run_id)
        os.killpg(int(metadata["process_group_id"]), signal.SIGKILL)
        console.print(f"Killed {run_id}")


@app.command()
def scan() -> None:
    """Placeholder safe scan: intentionally does not inspect or control real agent processes in v0."""
    table = Table(title="agentacct v0 scan")
    table.add_column("Scope")
    table.add_column("Behavior")
    table.add_row("real agent processes", "not inspected in v0 without explicit future approval")
    table.add_row("sentinel-owned runs", "use report/pause/resume/kill by run_id")
    console.print(table)


@app.command("agent-loop-demo")
def agent_loop_demo(
    store_dir: Annotated[Optional[Path], typer.Option(help="State directory for the demo summary. Defaults to a THROWAWAY temporary store; pass --store-dir to keep the summary.")] = None,
    run_id: Annotated[str, typer.Option(help="Run ID for the demo summary.")] = "run_agent_loop_demo",
    checkpoint_every_steps: Annotated[int, typer.Option(help="Pause/report after N visible outer-loop agent steps.")] = 3,
    on_checkpoint: Annotated[str, typer.Option(help="pause or report; pause stops at checkpoint, report continues.")] = "pause",
) -> None:
    """Run a local mock agent-loop demo that uses checkpoint language instead of hard max-steps."""
    if on_checkpoint not in {"pause", "report"}:
        raise typer.BadParameter("on-checkpoint must be 'pause' or 'report'")
    resolved_store_dir, store_is_temporary = _resolve_scratch_store_dir(store_dir, label="agent-loop-demo")

    instructions = [
        "Break the Agent FinOps idea into product requirements.",
        "Sketch the minimum architecture.",
        "List budget/cutoff policies.",
        "Draft a developer CLI UX.",
    ]

    def request_step(step_number: int, state: str, instruction: str) -> dict[str, object]:
        return {
            "status_code": 200,
            "forwarded": True,
            "content": f"Mock step {step_number}: {instruction}",
            "actual_provider_cost_usd": 0.0,
        }

    result = run_agent_like_loop(
        task="Demonstrate checkpoint-based external agent loop control.",
        step_instructions=instructions,
        request_step=request_step,
        options=AgentLoopOptions(
            store_dir=resolved_store_dir,
            run_id=run_id,
            checkpoint_every_steps=checkpoint_every_steps,
            on_checkpoint=on_checkpoint,  # type: ignore[arg-type]
        ),
    )
    if store_is_temporary:
        console.print(f"Demo store is temporary and will vanish on cleanup/reboot: {resolved_store_dir}")
    console.print(f"Status: {result.status}")
    console.print(f"Reason: {result.reason}")
    console.print(f"Visible steps attempted: {result.steps_attempted}")
    console.print(f"Summary: {result.summary_path}")


@claude_code_hooks_app.command("pre-tool-use")
def claude_code_pre_tool_use(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help="State directory for the hook context bridge. Defaults to the nearest existing .agent-sentinel/state above the hook event cwd."),
    ] = None,
) -> None:
    """Evaluate a Claude Code PreToolUse-style JSON event from stdin.

    Also persists hook-provided session_id/transcript_path as the project's
    current client context so MCP sections can inherit real join ids.
    """
    try:
        raw = sys.stdin.read()
        decision = evaluate_stdin_json(raw)
        try:
            capture_claude_code_client_context(raw, store_dir=store_dir)
        except Exception:  # noqa: BLE001 - context capture must never affect the hook decision.
            pass
        try:
            # Observe only the tool CATEGORY (never the name/args) for the
            # Receipt's Actions dimension. Separate try/except so it can never
            # affect the decision or the context capture above.
            capture_tool_activity(raw, store_dir=store_dir)
        except Exception:  # noqa: BLE001 - activity capture must never affect the hook decision.
            pass
        print(json.dumps(decision, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - FAIL OPEN: a PreToolUse hook must NEVER block a tool call, even on an internal agentacct error. An observe-only recorder that can brick every tool call is worse than one that records nothing.
        print(json.dumps({"agent_sentinel": {"decision": "allow", "risk": "low", "reason": f"agentacct hook error ({type(exc).__name__}); failing open"}}, ensure_ascii=False))


@claude_code_hooks_app.command("post-tool-use")
def claude_code_post_tool_use(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help="State directory for the hook context bridge. Defaults to the nearest existing .agent-sentinel/state above the hook event cwd."),
    ] = None,
) -> None:
    """Observe a Claude Code PostToolUse JSON event from stdin.

    Read-only: when the tool was a recognized test / build / lint / typecheck
    command, spool the exit code the harness observed as an INDEPENDENT machine
    check (source client_hook) — the only local source that is not the agent's
    own word. Never blocks or modifies the tool result; always returns an empty
    response, even on error.
    """
    try:
        raw = sys.stdin.read()
        try:
            capture_mechanical_check(raw, store_dir=store_dir)
        except Exception:  # noqa: BLE001 - capture must never affect the tool result.
            pass
        print("{}")
    except Exception:  # noqa: BLE001 - FAIL OPEN: a PostToolUse hook must never disturb the tool result.
        print("{}")


@claude_code_hooks_app.command("session-start")
def claude_code_session_start(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help="State directory for the hook context bridge. Defaults to the nearest existing .agent-sentinel/state above the hook event cwd."),
    ] = None,
) -> None:
    """Respond to a Claude Code SessionStart JSON event from stdin.

    Root sessions: persists hook-provided session_id/transcript_path as the
    project's current client context (same bridge as pre-tool-use, but from
    the very start of the session) and returns additionalContext directing
    the agent to record its work as sections. Subagent sessions: no capture,
    empty response — the root session owns the semantic goal.
    """
    try:
        raw = sys.stdin.read()
        response = claude_session_start_response(raw)
        if response:
            # Subagent/malformed events return {} above AND skip capture: a burst
            # of subagent captures could evict the root session's context file
            # from the capped per-session dir.
            try:
                capture_claude_code_client_context(raw, store_dir=store_dir)
            except Exception:  # noqa: BLE001 - context capture must never affect the hook response.
                pass
        print(json.dumps(response, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - FAIL OPEN: a SessionStart error must never break session startup; emit the empty no-op response and exit 0.
        print("{}")


@claude_code_hooks_app.command("install")
def claude_code_install(
    project_dir: Annotated[Path, typer.Option(help="Project directory to receive project-local hook files.")] = Path("."),
    dry_run: Annotated[bool, typer.Option(help="Print files that would be written without creating them.")] = False,
    force: Annotated[bool, typer.Option(help="Overwrite existing project-local hook files.")] = False,
    store_dir: Annotated[
        Optional[Path],
        typer.Option(
            help="Embed an explicit --store-dir (absolute) into the hook command. Required for global-store installs: "
            "GUI-launched Claude Code sessions inherit neither shell env vars nor a predictable cwd."
        ),
    ] = None,
    user_settings_example: Annotated[
        bool,
        typer.Option(
            "--user-settings-example",
            help="Render the example settings with the wrapper's ABSOLUTE path (for user-level ~/.claude/settings.json) "
            "instead of $CLAUDE_PROJECT_DIR, and print the block. Never writes user-level files itself.",
        ),
    ] = False,
) -> None:
    """Create project-local Claude Code hook wrapper and example settings."""
    # Captured BEFORE the install writes anything: only a genuinely fresh
    # wrapper proves the install happened NOW (a --force overwrite of an
    # existing wrapper says nothing about when it was first installed).
    wrapper_existed_before = claude_code_hook_paths(project_dir)[0].exists()
    try:
        install = install_claude_code_hook(
            project_dir, dry_run=dry_run, force=force, store_dir=store_dir, user_settings_example=user_settings_example
        )
    except FileExistsError as exc:
        console.print(str(exc))
        raise typer.Exit(1) from exc
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(2) from exc
    prefix = "Dry run: would write" if dry_run else "Wrote"
    print(f"{prefix} hook wrapper: {install.hook_path}")
    print(f"{prefix} example settings: {install.settings_path}")
    if install.agentacct_executable:
        print(f"Hook wrapper executes agentacct via install-time absolute path: {install.agentacct_executable}")
    else:
        print(
            "Warning: no absolute agentacct executable could be resolved; the wrapper will rely on PATH at hook time, "
            "which Claude Code sessions may not share. Re-run this install from the environment where agentacct is "
            "installed, then check: agentacct hooks claude-code doctor"
        )
    print(f"Example settings invoke the wrapper with: {install.python_command}")
    if install.store_dir is not None:
        print(f"Hook command binds the store explicitly: --store-dir {install.store_dir}")
    if user_settings_example:
        print("User-level settings example (merge the \"hooks\" and \"env\" blocks into ~/.claude/settings.json yourself):")
        print(json.dumps(install.settings_example, indent=2))
    if not dry_run:
        print("Review the example settings before copying/merging them into an active Claude Code settings file.")
        print(
            'Merge the example\'s "env": {"ENABLE_TOOL_SEARCH": "auto"} block along with "hooks": without it '
            "the agentacct MCP tools stay deferred and un-primed Claude Code sessions record nothing."
        )
        # Marker into the SAME store the hook command was bound to when
        # --store-dir was given, else the TARGET project's existing store
        # (never the cwd's); fresh installs only; best-effort so the hook
        # install result never depends on the marker write.
        _record_instrumentation_marker_best_effort(
            store_dir,
            client="claude-code",
            surface="claude_code_hook",
            target_path=str(install.hook_path),
            target_project_dir=project_dir,
            fresh_install=not wrapper_existed_before,
        )


@claude_code_hooks_app.command("doctor")
def claude_code_doctor(
    project_dir: Annotated[Path, typer.Option(help="Project directory to inspect.")] = Path("."),
) -> None:
    """Check project-local Claude Code hook files and hook command resolvability."""
    checks = claude_code_hook_doctor_checks(project_dir)
    table = Table(title="Claude Code hook doctor")
    table.add_column("Status")
    table.add_column("Check")
    table.add_column("Details")
    for check in checks:
        table.add_row(check["status"], check["name"], check["details"])
    console.print(table)
    if any(check["status"] == "warn" for check in checks):
        console.print(
            "Hook commands must resolve without your shell profile PATH: Claude Code sessions "
            "(especially the desktop app) often run hooks in a minimal environment."
        )


@cost_app.command("status")
def cost_status(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    run_id: Annotated[Optional[str], typer.Option(help="Only show ledger events for this agentacct run ID.")] = None,
) -> None:
    """Show the local estimated cost ledger."""
    ledger = CostLedger(_resolve_cli_store_dir(store_dir).path, create=False)
    events = ledger.read_events(run_id=run_id)
    if run_id:
        console.print(f"Run ID: {run_id}")
    console.print(f"Estimated total cost: ${ledger.total_estimated_cost_usd(run_id=run_id):.6f}")
    console.print(f"Actual provider cost: ${ledger.total_actual_provider_cost_usd(run_id=run_id):.6f}")
    console.print(f"Billable cutoff total: ${ledger.total_billable_cost_usd(run_id=run_id):.6f}")
    console.print(f"Estimated total tokens: {ledger.total_estimated_tokens(run_id=run_id)}")
    console.print(f"Events: {len(events)}")
    if events:
        confidence_counts: dict[str, int] = {}
        cost_confidence_counts: dict[str, int] = {}
        for event in events:
            confidence = str(event.get("usage_confidence") or "unknown")
            confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1
            cost_confidence = str(event.get("cost_confidence") or "unknown")
            cost_confidence_counts[cost_confidence] = cost_confidence_counts.get(cost_confidence, 0) + 1
        console.print("Confidence:")
        for confidence, count in sorted(confidence_counts.items()):
            console.print(f"  {confidence}: {count}")
        console.print("Cost confidence:")
        for confidence, count in sorted(cost_confidence_counts.items()):
            console.print(f"  {confidence}: {count}")
    if events:
        latest = events[-1]
        console.print(f"Latest: {latest.get('provider')} {latest.get('model')} {latest.get('decision')}")


def _restore_pricing_catalog_env(previous_catalog_path: str | None) -> None:
    if previous_catalog_path is None:
        os.environ.pop(PRICING_CATALOG_PATH_ENV, None)
    else:
        os.environ[PRICING_CATALOG_PATH_ENV] = previous_catalog_path
    reset_pricing_catalog_cache()


def _download_litellm_pricing_snapshot(snapshot_path: Path, *, source_url: str) -> dict[str, object]:
    # Force-now refresh (no TTL check): the shared fetcher is a plain GET of
    # the public LiteLLM table with the 120 s slow-link timeout, and the write
    # is atomic (temp + os.replace) so a serve re-reader never sees a torn file.
    payload = fetch_litellm_model_cost_map(source_url)
    return write_litellm_pricing_snapshot(snapshot_path, payload, source_url=source_url)


@cost_app.command("pricing-catalog")
def cost_pricing_catalog(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help="State directory for the default local pricing snapshot. Resolved like every store command (flag, then AGENTACCT_STORE_DIR, then project walk-up); without a resolvable store the builtin catalog is used."),
    ] = None,
    catalog_path: Annotated[Optional[Path], typer.Option(help="Optional local agentacct or LiteLLM pricing catalog JSON path.")] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Download the latest LiteLLM pricing snapshot into the local pricing catalog path before inspecting.")] = False,
    source_url: Annotated[str, typer.Option(help="LiteLLM model cost map URL used with --refresh.")] = LITELLM_MODEL_COST_MAP_URL,
    provider: Annotated[Optional[str], typer.Option(help="Provider to look up, e.g. openai or claude-code.")] = None,
    model: Annotated[Optional[str], typer.Option(help="Model to look up.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON output.")] = False,
) -> None:
    """Inspect the local pricing catalog used for token-price estimates."""

    previous_catalog_path = os.environ.get(PRICING_CATALOG_PATH_ENV)
    # Read-only catalog lookups keep working without a store: on resolution
    # failure fall back to the builtin catalog instead of failing the command.
    try:
        effective_store_dir: Path | None = resolve_store_dir(store_dir).path
    except StoreResolutionError:
        effective_store_dir = None
    default_catalog_path = pricing_catalog_path_for_store(effective_store_dir)
    selected_catalog_path = catalog_path or default_catalog_path
    refresh_metadata = None
    if refresh:
        if selected_catalog_path is None:
            raise typer.BadParameter("--refresh needs --catalog-path or a project/store directory")
        try:
            refresh_metadata = _download_litellm_pricing_snapshot(selected_catalog_path, source_url=source_url)
        except Exception as exc:
            raise typer.BadParameter(f"could not refresh LiteLLM pricing snapshot: {exc}") from exc
    if catalog_path is not None:
        os.environ[PRICING_CATALOG_PATH_ENV] = str(catalog_path)
    elif selected_catalog_path is not None and selected_catalog_path.exists():
        activate_pricing_catalog_for_store(effective_store_dir, catalog_path=selected_catalog_path, override_env=True)
    reset_pricing_catalog_cache()
    try:
        catalog = pricing_catalog()
        payload: dict[str, object] = {
            "catalog_path": read_env_alias(PRICING_CATALOG_PATH_ENV),
            "default_catalog_path": str(default_catalog_path) if default_catalog_path is not None else None,
            "entry_count": len(catalog.entries()),
            "refresh": refresh_metadata,
            "source_counts": catalog.source_counts(),
        }
        if provider is not None or model is not None:
            if not provider or not model:
                raise typer.BadParameter("--provider and --model must be provided together")
            entry = model_pricing_entry(provider, model)
            payload["lookup"] = {
                "provider": provider,
                "model": model,
                "priced": entry is not None,
                "entry": entry.to_dict() if entry is not None else None,
            }
        if json_output:
            print(json.dumps(payload, sort_keys=True))
            return
        console.print(f"Pricing catalog entries: {payload['entry_count']}")
        catalog_path_text = payload.get("catalog_path") or "builtin only"
        console.print(f"Catalog path: {catalog_path_text}")
        if isinstance(refresh_metadata, dict):
            console.print(f"Refreshed LiteLLM snapshot: {refresh_metadata.get('entry_count')} entry alias(es)")
        for source, count in sorted(catalog.source_counts().items()):
            console.print(f"  {source}: {count}")
        lookup = payload.get("lookup")
        if isinstance(lookup, dict):
            if lookup.get("priced"):
                entry = lookup["entry"]
                console.print(f"Lookup: priced by {entry['source']} ({entry['provider']} {entry['model']})")
            else:
                console.print("Lookup: unpriced")
    finally:
        _restore_pricing_catalog_env(previous_catalog_path)


@cost_app.command("subscription-set")
def cost_subscription_set(
    name: Annotated[str, typer.Option(help="Local subscription name, e.g. claude-code-pro or codex-pro.")],
    provider: Annotated[str, typer.Option(help="Provider/client label, e.g. claude-code or codex.")],
    monthly_price_usd: Annotated[float, typer.Option(help="User-entered monthly subscription price.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    period_days: Annotated[int, typer.Option(help="Subscription period length in days.")] = 30,
    period_run_budget: Annotated[Optional[int], typer.Option(help="Optional expected runs per period for allocation.")] = None,
    period_token_budget: Annotated[Optional[int], typer.Option(help="Optional expected tokens per period for allocation.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Save a local subscription plan for approximate allocation only."""
    name = _limited_text(name, field="name", max_length=80) or ""
    provider = _limited_text(provider, field="provider", max_length=80) or ""
    if not name or not provider:
        raise typer.BadParameter("--name and --provider must be non-empty")
    if not math.isfinite(monthly_price_usd) or monthly_price_usd <= 0:
        raise typer.BadParameter("--monthly-price-usd must be a finite value > 0")
    if period_days <= 0:
        raise typer.BadParameter("--period-days must be > 0")
    if period_run_budget is not None and period_run_budget <= 0:
        raise typer.BadParameter("--period-run-budget must be > 0")
    if period_token_budget is not None and period_token_budget <= 0:
        raise typer.BadParameter("--period-token-budget must be > 0")
    plan = SubscriptionPlan(
        name=name,
        provider=provider,
        monthly_price_usd=monthly_price_usd,
        period_days=period_days,
        period_run_budget=period_run_budget,
        period_token_budget=period_token_budget,
    )
    saved = SubscriptionStore(_resolve_cli_store_dir(store_dir).path).set_plan(plan)
    if json_output:
        print(json.dumps({"subscription": saved}, indent=2, sort_keys=True, allow_nan=False))
        return
    console.print(f"Saved subscription {name}: ${monthly_price_usd:.2f} / {period_days} days ({provider})")
    console.print("This is user-entered allocation data, not provider billing sync.")


@cost_app.command("subscription-list")
def cost_subscription_list(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """List saved local subscription allocation plans."""
    plans = SubscriptionStore(_resolve_cli_store_dir(store_dir).path, create=False).list_plans()
    if json_output:
        print(json.dumps({"subscriptions": plans}, indent=2, sort_keys=True, allow_nan=False))
        return
    if not plans:
        console.print("No subscriptions configured.")
        return
    for plan in plans:
        console.print(f"{plan.get('name')}: ${float(plan.get('monthly_price_usd') or 0.0):.2f} / {plan.get('period_days')} days ({plan.get('provider')})")


@cost_app.command("subscription-estimate")
def cost_subscription_estimate(
    subscription_price_usd: Annotated[Optional[float], typer.Option(help="User-entered subscription price for the period, e.g. 20 for a monthly plan.")] = None,
    subscription_name: Annotated[Optional[str], typer.Option(help="Use a saved subscription plan from cost subscription-set.")] = None,
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    run_id: Annotated[Optional[str], typer.Option(help="Only estimate from cost events for this agentacct run ID.")] = None,
    period_days: Annotated[int, typer.Option(help="Subscription period length in days. Used for labeling only; no billing is inferred.")] = 30,
    period_run_budget: Annotated[Optional[int], typer.Option(help="Approximate number of agentacct-observed runs you expect in this subscription period.")] = None,
    period_token_budget: Annotated[Optional[int], typer.Option(help="Approximate token budget/quota you want to allocate the subscription across.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Approximate subscription-cost allocation from local ledger events.

    This does not read provider billing and never reports exact Claude Code/Codex
    subscription spend. It only amortizes a user-entered subscription price over
    a user-entered denominator.
    """
    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    plan_name = _limited_text(subscription_name, field="subscription_name", max_length=80) if subscription_name else None
    if plan_name:
        plan = SubscriptionStore(resolved_store_dir, create=False).get_plan(plan_name)
        if plan is None:
            raise typer.BadParameter(f"unknown subscription plan: {plan_name}")
        subscription_price_usd = float(plan.get("monthly_price_usd") or 0.0)
        period_days = int(plan.get("period_days") or period_days)
        period_run_budget = period_run_budget if period_run_budget is not None else plan.get("period_run_budget")
        period_token_budget = period_token_budget if period_token_budget is not None else plan.get("period_token_budget")
    if subscription_price_usd is None or not math.isfinite(subscription_price_usd) or subscription_price_usd <= 0:
        raise typer.BadParameter("--subscription-price-usd must be a finite value > 0 or use --subscription-name")
    if period_days <= 0:
        raise typer.BadParameter("--period-days must be > 0")
    if period_run_budget is not None and period_run_budget <= 0:
        raise typer.BadParameter("--period-run-budget must be > 0")
    if period_token_budget is not None and period_token_budget <= 0:
        raise typer.BadParameter("--period-token-budget must be > 0")
    run_id = _validated_optional_run_id(run_id)
    ledger = CostLedger(resolved_store_dir, create=False)
    estimate = estimate_subscription_cost(
        ledger.read_events(run_id=run_id),
        subscription_price_usd=subscription_price_usd,
        period_days=period_days,
        run_id=run_id,
        period_run_budget=period_run_budget,
        period_token_budget=period_token_budget,
    )
    payload = estimate.to_dict()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
        return
    console.print("Approximate subscription cost allocation")
    if run_id:
        console.print(f"Run ID: {run_id}")
    console.print(f"Subscription price: ${subscription_price_usd:.2f} per {period_days} days")
    console.print(f"Observed events: {payload['event_count']}")
    console.print(f"Observed runs: {payload['observed_run_count']}")
    console.print(f"Estimated tokens: {payload['estimated_total_tokens']}")
    for allocation in payload["allocations"]:
        cost = allocation["allocated_cost_usd"]
        cost_text = "unavailable" if cost is None else f"${float(cost):.6f}"
        console.print(f"{allocation['method']}: {cost_text} ({allocation['confidence']})")
        console.print(f"  {allocation['reason']}")
    console.print(payload["note"])


@setup_app.command("prompt")
def setup_prompt(
    agent: Annotated[str, typer.Option(help="Agent client for the install prompt: claude-code, codex, generic, hermes, opencode, or openclaw.")],
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help=(
                "Print the self-contained offline install prompt instead of the short one-liner. "
                "Use when the target agent cannot fetch INSTALL.md from the network."
            ),
        ),
    ] = False,
) -> None:
    """Print a copy/paste prompt that asks a coding agent to install agentacct.

    The default output is the short one-line prompt pointing at INSTALL.md
    (the canonical agent-facing runbook). --full emits an offline prompt built
    from the same install_guide content, so the two cannot drift.
    """
    if agent not in MCP_SETUP_AGENTS:
        raise typer.BadParameter("agent must be one of: claude-code, codex, generic, hermes, opencode, openclaw")
    if full:
        print(install_guide_full_prompt(agent).rstrip())
    else:
        print(install_guide_one_line_prompt(agent))


# Instrumentation markers: CLI-authored "recording was installed for this
# client at this time" facts, so the ledger can classify sessions as pre/post
# instrumentation instead of rendering pre-install history as product failure.
# metadata.client uses the SAME vocabulary as usage-row metadata.client
# (SUPPORTED_CLIENTS), or per-client isolation in the ledger would never match.
# frozen: historical stores/logs/files carry this source string forever
# (usage_truth matchers classify sessions by it); never rename.
INSTRUMENTATION_MARKER_SOURCE = "agent-sentinel-setup"
INSTRUMENTATION_MARKER_SURFACES = ("instructions_user", "instructions_project", "claude_code_hook")


def _record_instrumentation_marker(
    resolved_store_dir: Path,
    *,
    client: str,
    surface: str,
    installed_at: float,
    installed_at_source: str,
    target_path: str | None,
) -> dict:
    """Write one CLI-authored instrumentation marker event into the store.

    command_time idempotency is a PRE-SCAN for a GENUINE (provenance-bearing)
    marker with the same client+surface: a re-run returns the ORIGINAL event
    so the earliest honest install time is preserved. Service-level
    idempotency keys are deliberately NOT used — an agent/HTTP event
    pre-seeded with the key keeps the key (only the provenance is stripped),
    so the keyed lookup would match the forgery on every future run and
    permanently defeat marker idempotency. Backfill markers are deliberate
    rewrites of history and always append. target_path stays in the local
    store only.
    """

    service = SentinelService(resolved_store_dir)
    if installed_at_source == "command_time":
        for event in service.list_all_events():
            existing_metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            if (
                is_instrumentation_marker_event(event)
                and existing_metadata.get("client") == client
                and existing_metadata.get("surface") == surface
                and existing_metadata.get("installed_at_source") == "command_time"
            ):
                # File order is append order: the first match is the earliest.
                return event
    metadata: dict[str, object] = {
        "client": client,
        "installed_at": installed_at,
        "installed_at_source": installed_at_source,
        "surface": surface,
    }
    if target_path is not None:
        metadata["target_path"] = target_path
    return service.record_event(
        {
            "source": INSTRUMENTATION_MARKER_SOURCE,
            "event_type": INSTRUMENTATION_MARKER_EVENT_TYPE,
            "run_id": None,
            "provider": None,
            "model": None,
            "estimated_input_tokens": None,
            "estimated_output_tokens": None,
            "estimated_cost_usd": None,
            "usage_confidence": None,
            "cost_confidence": None,
            "metadata": metadata,
        },
        trusted_instrumentation_marker=True,
    )


def _install_marker_store_dir(store_dir: Path | None, *, target_project_dir: Path | None) -> Path | None:
    """Store that should receive an install-time marker, derived from the
    INSTALL TARGET, never the cwd.

    Explicit ``--store-dir`` wins. Otherwise only the target project's
    EXISTING ``.agent-sentinel/state`` counts — no cwd walk-up (which could
    stamp an unrelated project's history) and no store creation. ``None``
    means "no honest home for the marker": user-level surfaces span projects,
    so only an explicit store can receive their marker.
    """
    if store_dir is not None:
        return resolve_store_dir(store_dir).path
    if target_project_dir is not None:
        candidate = Path(target_project_dir).expanduser() / ".agent-sentinel" / "state"
        if candidate.is_dir():
            return candidate
    return None


def _store_has_genuine_marker(resolved_store_dir: Path, *, client: str) -> bool:
    try:
        events = SentinelService(resolved_store_dir, create=False).list_all_events()
    except OSError:
        return False
    return any(
        is_instrumentation_marker_event(event)
        and isinstance(event.get("metadata"), dict)
        and event["metadata"].get("client") == client
        for event in events
    )


def _record_instrumentation_marker_best_effort(
    store_dir: Path | None,
    *,
    client: str,
    surface: str,
    target_path: str | None,
    target_project_dir: Path | None = None,
    fresh_install: bool = True,
) -> None:
    """Best-effort marker write for install commands: never fails the install.

    Two honesty gates (missing beats wrong):
    - the marker store derives from the INSTALL TARGET
      (``_install_marker_store_dir``), never a cwd walk-up — a marker in the
      wrong project's store rewrites THAT project's pre/post history;
    - only a FRESH install may auto-record command_time: re-running over an
      already-installed surface proves nothing about WHEN it was first
      installed, and stamping now would flip genuinely-instrumented history
      to pre_instrumentation (false claims, the flattering direction).
    Every skipped path where no genuine marker covers the client prints ONE
    actionable hint (counters over silence) and the command still exits 0.
    """
    resolved = _install_marker_store_dir(store_dir, target_project_dir=target_project_dir)
    if not fresh_install:
        if resolved is not None and _store_has_genuine_marker(resolved, client=client):
            return  # a genuine marker already covers this client: nothing to add, no noise.
        print(
            f"Note: {surface} for {client} was already installed, so no command-time instrumentation marker was "
            "recorded (it would falsely date the install as now). Backfill the honest time with: "
            f"agentacct setup mark-instrumented --client {client} --surface {surface} "
            "--installed-at <when it was first installed> --store-dir <path>"
        )
        return
    if resolved is None:
        print(
            "Note: no instrumentation marker was recorded — no store is bound to this install target "
            "(the marker never falls back to the current directory's store; user-level surfaces span projects). "
            f"Record it explicitly with: agentacct setup mark-instrumented --client {client} "
            f"--surface {surface} --store-dir <path>"
        )
        return
    try:
        _record_instrumentation_marker(
            resolved,
            client=client,
            surface=surface,
            installed_at=time.time(),
            installed_at_source="command_time",
            target_path=target_path,
        )
    except OSError as exc:
        print(
            f"Note: could not record the instrumentation marker ({exc}). Record it explicitly with: "
            f"agentacct setup mark-instrumented --client {client} --surface {surface} --store-dir <path>",
            file=sys.stderr,
        )
        return
    print(f"Recorded instrumentation marker: client={client} surface={surface}")


@setup_app.command("mark-instrumented")
def setup_mark_instrumented(
    client: Annotated[str, typer.Option(help="Client the instrumentation was installed for: " + ", ".join(SUPPORTED_CLIENTS) + ".")],
    surface: Annotated[
        str,
        typer.Option(help="Where recording was installed: instructions_user, instructions_project, or claude_code_hook."),
    ] = "instructions_user",
    installed_at: Annotated[
        Optional[str],
        typer.Option(
            "--installed-at",
            help=(
                "Backfill the honest install time as ISO-8601 WITH an explicit UTC offset "
                "(e.g. 2026-07-07T19:57:00+08:00). Naive or future timestamps are rejected. "
                "Omit to record now (idempotent per client+surface)."
            ),
        ),
    ] = None,
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Record when agentacct instrumentation was installed for a client.

    The ledger classifies sessions before this moment as pre-instrumentation
    (they could never have MCP work context) instead of rendering pre-install
    history as missing context. `setup instructions` and `hooks claude-code
    install` record this automatically; use this command for stores those
    commands could not reach, or with --installed-at to backfill the real
    install time of an older setup.
    """
    if client not in SUPPORTED_CLIENTS:
        raise typer.BadParameter("client must be one of: " + ", ".join(SUPPORTED_CLIENTS))
    if surface not in INSTRUMENTATION_MARKER_SURFACES:
        raise typer.BadParameter("surface must be one of: " + ", ".join(INSTRUMENTATION_MARKER_SURFACES))
    if installed_at is None:
        installed_epoch = time.time()
        installed_at_source = "command_time"
    else:
        try:
            parsed = datetime.fromisoformat(installed_at)
        except ValueError as exc:
            raise typer.BadParameter(f"--installed-at is not valid ISO-8601: {installed_at}") from exc
        if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
            raise typer.BadParameter(
                "--installed-at must carry an explicit UTC offset (e.g. 2026-07-07T19:57:00+08:00); "
                "a naive timestamp would silently shift the pre/post boundary with the machine's timezone."
            )
        installed_epoch = parsed.timestamp()
        if installed_epoch > time.time():
            raise typer.BadParameter("--installed-at is in the future; markers record when install actually happened.")
        installed_at_source = "backfill"
    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    recorded = _record_instrumentation_marker(
        resolved_store_dir,
        client=client,
        surface=surface,
        installed_at=installed_epoch,
        installed_at_source=installed_at_source,
        target_path=None,
    )
    if json_output:
        print(json.dumps({"event": recorded}, indent=2, sort_keys=True))
        return
    print(f"Recorded instrumentation marker {recorded['event_id']}: client={client} surface={surface} ({installed_at_source})")
    if installed_at_source == "command_time" and abs(float(recorded["metadata"].get("installed_at") or 0.0) - installed_epoch) > 1.0:
        # Idempotent re-run returned the original event: say so instead of
        # implying a new marker was written.
        print("An earlier marker for this client+surface already existed; the original (earliest) install time is kept.")


def _instruction_target_path(agent: str, *, user: bool, path: Path | None) -> Path:
    """Resolve the instruction file for `setup instructions`.

    --path always wins (used by tests and non-standard layouts). Otherwise the
    per-agent default: user-level under $HOME (default), or project-level under
    the current directory with --user omitted... but the common ask is user
    level, so `--user` selects the home-dir file and the bare default is the
    project-level file in the current repo.
    """
    if path is not None:
        return path.expanduser()
    if user:
        return Path.home() / install_guide.INSTRUCTION_USER_FILES[agent]
    return Path.cwd() / install_guide.INSTRUCTION_PROJECT_FILES[agent]


@setup_app.command("instructions")
def setup_instructions(
    agent: Annotated[str, typer.Option(help="Agent whose instruction file to edit: claude-code or codex.")],
    user: Annotated[
        bool,
        typer.Option(
            "--user",
            help="Target the user-level instruction file (~/.claude/CLAUDE.md or ~/.codex/AGENTS.md) instead of the project-level file in the current directory.",
        ),
    ] = False,
    path: Annotated[
        Optional[Path],
        typer.Option(help="Explicit instruction file path (overrides --agent/--user defaults). Intended for tests and non-standard layouts."),
    ] = None,
    remove: Annotated[bool, typer.Option("--remove", help="Strip the managed agentacct block instead of adding/updating it.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print a unified diff of the change and write nothing.")] = False,
    store_dir: Annotated[
        Optional[Path],
        typer.Option(
            help="Store that should receive the instrumentation marker (e.g. an explicit global store). "
            "Defaults to the target project's existing store for project-level installs; --user/--path installs "
            "skip the marker without an explicit store. Best-effort, never fails the install."
        ),
    ] = None,
) -> None:
    """Merge standing 'record your work as sections' instructions into an agent
    instruction file, idempotently.

    The instruction text lives between `<!-- agent-chronicle:begin -->` and
    `<!-- agent-chronicle:end -->` markers: re-running replaces ONLY that block,
    `--remove` strips it, and content outside the markers is never touched. This
    is what makes agents in every session record work sections so the dashboard
    (especially in global mode) fills with work context instead of tokens alone.
    """
    if agent not in install_guide.INSTRUCTION_AGENTS:
        raise typer.BadParameter("agent must be one of: claude-code, codex")
    target = _instruction_target_path(agent, user=user, path=path)
    # The marker records the honest fact "instructions are installed from this
    # moment": it is written only on a FRESH install (the target did not carry
    # the managed block before this run) — a no-change re-run or a block
    # update proves nothing about WHEN the install happened, so those paths
    # skip and hint at `setup mark-instrumented --installed-at` instead.
    # Never on --dry-run or --remove. The marker store derives from the
    # INSTALL TARGET: --store-dir wins; the project-level default uses the
    # existing store next to the target file; --user/--path have no honest
    # default store (a user-level surface spans projects).
    instruction_surface = "instructions_user" if user else "instructions_project"
    marker_target_project_dir = None if (user or path is not None) else target.parent
    existing_text = target.read_text(encoding="utf-8") if target.exists() else ""
    fresh_install = not install_guide.instruction_file_has_managed_block(existing_text)
    new_text = install_guide.render_instruction_file(existing_text, remove=remove)

    if new_text == existing_text:
        action = "remove" if remove else "update"
        print(f"No change: {target} already has the {'block removed' if remove else 'current block'} (idempotent {action}).")
        if not remove and not dry_run:
            _record_instrumentation_marker_best_effort(
                store_dir,
                client=agent,
                surface=instruction_surface,
                target_path=str(target),
                target_project_dir=marker_target_project_dir,
                fresh_install=fresh_install,
            )
        return

    if dry_run:
        import difflib

        diff = difflib.unified_diff(
            existing_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{target.name}",
            tofile=f"b/{target.name}",
        )
        rendered = "".join(diff)
        print(f"Dry run: would {'remove the block from' if remove else 'write'} {target}")
        if rendered:
            print(rendered, end="" if rendered.endswith("\n") else "\n")
        return

    if remove and not target.exists():
        print(f"No change: {target} does not exist.")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8")
    if remove:
        print(f"Removed the agentacct instruction block from {target}.")
    else:
        verb = "Updated" if existing_text else "Wrote"
        print(f"{verb} the agentacct instruction block in {target} (managed between the agent-chronicle:begin/end markers).")
        _record_instrumentation_marker_best_effort(
            store_dir,
            client=agent,
            surface=instruction_surface,
            target_path=str(target),
            target_project_dir=marker_target_project_dir,
            fresh_install=fresh_install,
        )


@setup_app.command("mcp")
def setup_mcp(
    agent: Annotated[str, typer.Option(help="Agent client to configure: claude-code, codex, generic, hermes, opencode, or openclaw.")],
    project_dir: Annotated[Path, typer.Option(help="Project directory that should own local agentacct state.")] = Path("."),
    store_dir: Annotated[Optional[Path], typer.Option(help="Override agentacct state directory for the MCP server.")] = None,
    mcp_command: Annotated[
        Optional[str],
        typer.Option(help="Command path to write/print in MCP config. Use when agentacct is not on the agent's PATH."),
    ] = None,
    write: Annotated[bool, typer.Option(help="Write supported project-local configuration files. Default only previews safe copy/paste instructions.")] = False,
    relative_store_path: Annotated[
        bool,
        typer.Option(
            "--relative-store-path",
            help=(
                "Write the relative '.agent-sentinel/state' store path into MCP config instead of the absolute default. "
                "Use for configs committed and shared across machines; `mcp serve` resolves it against the project root."
            ),
        ),
    ] = False,
) -> None:
    """Generate safe MCP setup instructions for coding agents."""
    project_dir = project_dir.resolve()
    mcp_command = _resolve_mcp_command(mcp_command)
    if relative_store_path and store_dir is not None:
        raise typer.BadParameter("--relative-store-path cannot be combined with --store-dir")
    effective_store_dir = _mcp_store_dir_for_project(project_dir, store_dir)
    # Absolute by default: a relative config path is resolved against whatever
    # cwd the MCP client launches the server with, silently creating stray
    # stores. --relative-store-path opts back into the portable relative form
    # (now safe-ish: `mcp serve` resolves it against the project root).
    config_store_dir: Path | str = ".agent-sentinel/state" if relative_store_path else effective_store_dir
    if agent not in MCP_SETUP_AGENTS:
        raise typer.BadParameter("agent must be one of: claude-code, codex, generic, hermes, opencode, openclaw")

    console.print("agentacct MCP setup")
    console.print("Source: PyPI (pipx install agentacct)")
    console.print(f"Store dir: {effective_store_dir}")
    console.print(f"Config store arg: {config_store_dir}")
    console.print(f"MCP command: {mcp_command} mcp serve")
    worktree_owner = claude_worktree_owner_dir(project_dir) if store_dir is None else None
    if worktree_owner is not None:
        if effective_store_dir == (worktree_owner.resolve() / ".agent-sentinel" / "state").resolve():
            console.print("Claude Code worktree detected.")
            console.print(
                "Using the owning project's store for MCP config: a worktree-local store path would vanish "
                "with the worktree and re-create phantom stores from committed config."
            )
        elif agent == "claude-code":
            # The worktree has its own pre-existing store, which wins
            # (consistent with resolve_store_dir); still suggest the stable
            # owner store for committed config.
            _print_claude_worktree_store_hint(project_dir, command=mcp_command)

    if agent in {"generic", "hermes", "opencode", "openclaw"}:
        _print_agent_mcp_preview(agent, config_store_dir, command=mcp_command)
        if write:
            console.print("--write is not available for this agent because its MCP config is profile/global or client-specific.")
            console.print("Use the preview command above, then run: agentacct mcp doctor")
            raise typer.Exit(1)
        console.print("Preview only. agentacct will not modify global/profile agent config.")
        return

    if agent == "claude-code":
        _print_claude_mcp_setup(config_store_dir, command=mcp_command)
        if write:
            config_path, action = _write_claude_mcp_config(project_dir, config_store_dir, command=mcp_command)
            if action != "skipped":
                console.print(f"Wrote Claude Code MCP config: {config_path}")
        else:
            console.print("Preview only. Re-run with --write to create/update .mcp.json.")
        return

    _print_codex_mcp_setup(config_store_dir, command=mcp_command)
    if write:
        config_path, action = _write_codex_mcp_config(project_dir, config_store_dir, command=mcp_command)
        if action != "skipped":
            console.print(f"{action.capitalize()} Codex MCP config block: {config_path}")
    else:
        console.print("Preview only. Re-run with --write to create/update project .codex/config.toml.")


@mcp_app.command("serve")
def mcp_serve(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP + " Relative values (legacy configs) are resolved against the nearest project root, not the MCP client's launch cwd.")] = None,
) -> None:
    """Serve agentacct MCP tools over stdio.

    This exposes safe report/outcome/value primitives and does not call paid judge APIs.
    Store resolution stays strict — the MCP client controls this process's cwd,
    so a silently wrong ledger is never acceptable — but a server that EXITS reads
    to the host as a crash. So an unresolvable store starts a degraded-but-connected
    session instead: initialize and tools/list still answer, recording tools return
    a legible "no store configured" error, and no store is ever silently created.
    """
    # As an MCP server this process's exit is the ONLY liveness signal the host
    # has: an exit at startup reads as a crash. So an unresolvable store must
    # NOT exit — it starts a degraded-but-connected session (initialize +
    # tools/list still answer; recording tools return a legible error). We still
    # never silently create or pick a store (honesty rule): degraded_reason is
    # set and no store path is resolved.
    resolved: Path | None = None
    degraded_reason: str | None = None
    if store_dir is None:
        try:
            resolved = resolve_store_dir(None).path
        except StoreResolutionError as exc:
            print(
                f"agentacct mcp serve: {exc}\n"
                "Starting a degraded MCP session (connected, but not recording): restart the server with "
                f"--store-dir <absolute path> or set {ENV_STORE_DIR}=<absolute path>.",
                file=sys.stderr,
            )
            degraded_reason = DEGRADED_NO_STORE_MESSAGE
    else:
        expanded = store_dir.expanduser()
        if expanded.is_absolute():
            resolved = expanded
        else:
            # Legacy relative config value: anchor it to the nearest project
            # root (worktree-remapped walk-up), NEVER the raw client cwd —
            # that is how stray stores were silently created. The env var is
            # deliberately ignored for ANCHORING (it names a store, not a
            # project root), but when no project root exists a valid absolute
            # AGENTACCT_STORE_DIR wins outright — the user explicitly
            # named the store, which beats a dead server.
            try:
                resolution = resolve_store_dir(None, env={})
            except StoreResolutionError:
                try:
                    env_value = store_env_dir_value(os.environ) or ""
                except StoreResolutionError:
                    env_value = ""
                env_path = Path(env_value).expanduser() if env_value else None
                if env_path is not None and env_path.is_absolute():
                    print(
                        f"agentacct mcp serve: relative --store-dir {store_dir} is a legacy config form and no "
                        f"project store was found above the MCP client's working directory; using {ENV_STORE_DIR}={env_path} instead.",
                        file=sys.stderr,
                    )
                    resolved = env_path
                else:
                    print(
                        f"agentacct mcp serve: relative --store-dir {store_dir} is a legacy config form and no project "
                        "store was found above the MCP client's working directory; refusing to create a stray store.",
                        file=sys.stderr,
                    )
                    if env_value:
                        print(
                            f"Note: {ENV_STORE_DIR}={env_value} is set but not an absolute path, so it was ignored.",
                            file=sys.stderr,
                        )
                    print(
                        "Fix: re-run `agentacct setup mcp --agent <claude-code|codex> --write` in the project root to "
                        f"write an absolute store path into the MCP config, or set {ENV_STORE_DIR}=<absolute path>. "
                        "Starting a degraded MCP session (connected, but not recording) until then.",
                        file=sys.stderr,
                    )
                    degraded_reason = DEGRADED_NO_STORE_MESSAGE
            else:
                anchor = resolution.project_root if resolution.project_root is not None else resolution.path.parent.parent
                resolved = anchor / expanded
    if degraded_reason is not None:
        # No resolvable store: stay connected but refuse to record. serve_stdio
        # gets store_dir=None so it never constructs (or creates) a store.
        serve_stdio(store_dir=None, degraded_reason=degraded_reason)
        return
    print(f"agentacct mcp serve: store={resolved}", file=sys.stderr)
    serve_stdio(store_dir=resolved)


def _mcp_config_store_dir_checks(project_root: Path, resolved_store: Path) -> list[dict[str, str]]:
    """Warn-level checks for --store-dir values embedded in project MCP config."""
    checks: list[dict[str, str]] = []

    def _check_args(config_label: str, args: object, *, agent: str) -> None:
        if not isinstance(args, list):
            return
        values = [str(item) for item in args]
        if "--store-dir" not in values:
            return
        index = values.index("--store-dir")
        if index + 1 >= len(values):
            return
        raw_value = values[index + 1]
        value_path = Path(raw_value).expanduser()
        if not value_path.is_absolute():
            checks.append(
                {
                    "name": f"mcp config ({config_label})",
                    "status": "warn",
                    "details": (
                        f"legacy relative store path {raw_value!r}; works when the MCP client starts inside this project, "
                        f"but re-run: agentacct setup mcp --agent {agent} --write for a robust absolute path"
                    ),
                }
            )
        elif value_path != resolved_store:
            checks.append(
                {
                    "name": f"mcp config ({config_label})",
                    "status": "warn",
                    "details": f"config store {value_path} differs from the resolved store {resolved_store}",
                }
            )
        else:
            checks.append({"name": f"mcp config ({config_label})", "status": "ok", "details": f"store path matches: {value_path}"})

    # Probe EVERY registration key: new installs write "agentacct", but
    # pre-rename "agent-chronicle" / "agent-sentinel" registrations are equally
    # recognized forever (log-evidence pairing accepts them all), so doctor must
    # not false-negative on a legacy install — and must say which name is
    # registered.
    claude_config = project_root / ".mcp.json"
    if claude_config.is_file():
        try:
            payload = json.loads(claude_config.read_text(encoding="utf-8"))
            for server_key in ("agentacct", "agent-chronicle", "agent-sentinel"):
                server = payload.get("mcpServers", {}).get(server_key) if isinstance(payload, dict) else None
                if isinstance(server, dict):
                    _check_args(f".mcp.json [{server_key}]", server.get("args"), agent="claude-code")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            checks.append({"name": "mcp config (.mcp.json)", "status": "warn", "details": f"unreadable config: {exc}"})
    codex_config = project_root / ".codex" / "config.toml"
    if codex_config.is_file():
        try:
            payload = tomllib.loads(codex_config.read_text(encoding="utf-8"))
            for server_key in ("agentacct", "agent-chronicle", "agent-sentinel"):
                server = payload.get("mcp_servers", {}).get(server_key)
                if isinstance(server, dict):
                    _check_args(f".codex/config.toml [{server_key}]", server.get("args"), agent="codex")
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            checks.append({"name": "mcp config (.codex/config.toml)", "status": "warn", "details": f"unreadable config: {exc}"})
    return checks


def _warn_dashboard_mcp_store_shadow(global_store: Path) -> None:
    """Warn when project MCP config can keep writing outside the product home.

    This is deliberately diagnostic-only: silently rewriting or deleting a
    user's project MCP registration would be an unsafe ledger migration, and a
    running agent session cannot hot-swap the MCP process anyway.
    """

    try:
        project_resolution = resolve_store_dir(None, env={})
    except StoreResolutionError:
        return
    if project_resolution.source != "project" or project_resolution.project_root is None:
        return
    mismatches = [
        check
        for check in _mcp_config_store_dir_checks(project_resolution.project_root, global_store)
        if check.get("status") == "warn"
    ]
    if not mismatches:
        return
    console.print(
        "[yellow]Warning:[/yellow] project MCP config can shadow the user-level global MCP server, so new work may "
        "be recorded outside this All projects dashboard."
    )
    for check in mismatches:
        console.print(f"- {check['details']}")
    console.print(
        "Align or remove the project MCP registration, then open a new agent session; already-running sessions "
        "keep their current MCP store."
    )


def _mcp_doctor_write_probe() -> tuple[dict[str, object], dict[str, str]]:
    """Round-trip a test event in a THROWAWAY temp store. Never the real ledger."""
    # frozen prefix: pre-rename doctor-probe naming kept for continuity with
    # historical logs/scripts (throwaway temp store either way).
    temp_store = tempfile.mkdtemp(prefix="agent-sentinel-doctor-probe-")
    try:
        server = SentinelMCPServer(store_dir=temp_store)
        result = server.call_tool(
            "agentacct_record_event",
            {
                # Diagnostic signature: this type/source pair MUST stay
                # registered in usage_truth.DIAGNOSTIC_EVENT_TYPES/_SOURCES or
                # probe events leak into user-facing ledger views.
                "source": "agent-sentinel-mcp-doctor",
                "event_type": "mcp_doctor_test",
                "run_id": "mcp_doctor_test",
                "metadata": {"summary": "safe local MCP doctor test event (throwaway temp store)"},
            },
        )
        listed = server.call_tool("agentacct_list_events", {"limit": 20, "run_id": "mcp_doctor_test"})
        recorded = json.loads(result["content"][0]["text"])["event"]
        listed_events = json.loads(listed["content"][0]["text"])["events"]
        recorded_event_id = recorded.get("event_id")
        round_trip_ok = bool(recorded_event_id) and any(event.get("event_id") == recorded_event_id for event in listed_events)
    except Exception as exc:  # noqa: BLE001 - the probe must report, not crash, doctor.
        return {"round_trip_ok": False, "temp_store": True}, {
            "name": "write probe",
            "status": "fail",
            "details": f"write probe failed in throwaway temp store: {exc}",
        }
    finally:
        shutil.rmtree(temp_store, ignore_errors=True)
    if round_trip_ok:
        return {"round_trip_ok": True, "temp_store": True}, {
            "name": "write probe",
            "status": "ok",
            "details": "round-trip ok (throwaway temp store, removed)",
        }
    return {"round_trip_ok": False, "temp_store": True}, {
        "name": "write probe",
        "status": "fail",
        "details": "test event did not round-trip (throwaway temp store, removed)",
    }


@mcp_app.command("doctor")
def mcp_doctor(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    write_probe: Annotated[
        bool,
        typer.Option(
            "--write-probe",
            help="Also round-trip a test event through the MCP write path in a THROWAWAY temp store. Never writes to the resolved store.",
        ),
    ] = False,
) -> None:
    """Run read-only MCP diagnostics: store resolution/readability, tool inventory, join health, hook context.

    The default run performs ZERO writes to any store. The optional write probe
    targets a throwaway temp store only — probing the production ledger is
    intentionally unsupported.
    """
    checks: list[dict[str, str]] = []
    store_info: dict[str, object] = {"path": None, "source": None, "exists": False, "event_count": 0, "parse_errors": 0}
    resolution: StoreResolution | None = None
    try:
        resolution = resolve_store_dir(store_dir)
    except StoreResolutionError as exc:
        checks.append({"name": "store resolution", "status": "fail", "details": str(exc)})

    events: list[dict[str, object]] = []
    join_health: dict[str, object] | None = None
    hook_status: dict[str, object] | None = None
    client_log_evidence: dict[str, object] | None = None
    if resolution is not None:
        store_root = resolution.path
        store_info["path"] = str(store_root)
        store_info["source"] = resolution.source
        checks.append({"name": "store resolution", "status": "ok", "details": f"{store_root} (source: {resolution.source})"})
        store_exists = store_root.is_dir()
        store_info["exists"] = store_exists
        events_path = store_root / "events.jsonl"
        if not store_exists:
            checks.append(
                {
                    "name": "store readability",
                    "status": "warn",
                    "details": "store not initialized (directory does not exist; run agentacct init in the project root)",
                }
            )
        elif not events_path.is_file():
            # A store cut over to the SQLite log has no events.jsonl; read its
            # ledger from events.sqlite3 instead of reporting the store empty.
            from .event_log import RAW_EVENT_LOG_FILENAME, RawEventLog

            log_path = store_root / RAW_EVENT_LOG_FILENAME
            if log_path.is_file():
                try:
                    events = RawEventLog(log_path).read_events()
                    store_info["event_count"] = len(events)
                    checks.append(
                        {
                            "name": "store readability",
                            "status": "ok",
                            "details": f"{len(events)} event(s) in the SQLite event log (events.jsonl retired)",
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - diagnose, never crash.
                    checks.append(
                        {"name": "store readability", "status": "fail", "details": f"events.sqlite3 is not readable: {exc}"}
                    )
                # Writability WITHOUT writing bytes: the SQLite log (WAL mode)
                # needs both the DB file and its directory writable to append.
                # Pure os.access permission checks keep the doctor read-only,
                # the same guarantee the events.jsonl append-probe gives — and a
                # SQLite-authoritative store (the default) must still be told
                # whether its ledger is writable.
                if os.access(log_path, os.W_OK) and os.access(store_root, os.W_OK):
                    checks.append(
                        {"name": "store writability", "status": "ok", "details": "events.sqlite3 is writable (no bytes were written)"}
                    )
                else:
                    checks.append(
                        {"name": "store writability", "status": "fail", "details": "events.sqlite3 (or its directory) is not append-writable"}
                    )
            else:
                checks.append({"name": "store readability", "status": "ok", "details": "store file absent (created on first recorded event)"})
        else:
            # A doctor must diagnose a broken store, not crash on it: an
            # unreadable events.jsonl (permissions, I/O error) becomes a
            # failing check — and --json still emits its full payload.
            try:
                raw_lines: list[str] | None = events_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                raw_lines = None
                checks.append({"name": "store readability", "status": "fail", "details": f"events.jsonl is not readable: {exc}"})
            if raw_lines is not None:
                parse_errors = 0
                for line in raw_lines:
                    if not line.strip():
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue
                    if isinstance(parsed, dict):
                        events.append(parsed)
                store_info["event_count"] = len(events)
                store_info["parse_errors"] = parse_errors
                checks.append(
                    {
                        "name": "store readability",
                        "status": "ok" if parse_errors == 0 else "warn",
                        "details": f"{len(events)} event(s), {parse_errors} parse error(s)",
                    }
                )
                # Append-permission probe WITHOUT writing bytes. Only when the
                # file already exists: append-open would otherwise create it.
                try:
                    with events_path.open("a", encoding="utf-8"):
                        pass
                    checks.append({"name": "store writability", "status": "ok", "details": "events.jsonl is append-writable (no bytes were written)"})
                except OSError as exc:
                    checks.append({"name": "store writability", "status": "fail", "details": f"events.jsonl is not append-writable: {exc}"})
        join_health = build_client_context_join_health(events)
        # Client-log evidenced links (read-only, derived fresh from trusted
        # import rows — install-time verification that log pairing works).
        evidence_index = build_log_evidence_index([event for event in events if isinstance(event, dict)])
        donor_summary = summarize_log_evidence_donor_rows([event for event in events if isinstance(event, dict)])
        client_log_evidence = {
            "donor_usage_rows": donor_summary["donor_usage_rows"],
            "evidenced_event_ids_total": donor_summary["evidenced_event_ids_total"],
            "evidenced_events_in_store": sum(
                1 for event in events if isinstance(event, dict) and str(event.get("event_id") or "") in evidence_index
            ),
            "outputs_skipped": donor_summary["outputs_skipped"],
        }
        hook_status = claude_code_hook_context_status(store_root)
        if resolution.source == "project" and resolution.project_root is not None:
            checks.extend(_mcp_config_store_dir_checks(resolution.project_root, store_root))

    tool_names = [tool["name"] for tool in TOOLS]
    checks.append({"name": "mcp tools", "status": "ok", "details": f"{len(tool_names)} tool(s) available"})

    probe_payload: dict[str, object] | None = None
    if write_probe:
        probe_payload, probe_check = _mcp_doctor_write_probe()
        checks.append(probe_check)

    failed = any(check["status"] == "fail" for check in checks)

    if json_output:
        payload = {
            "read_only": True,
            "store": store_info,
            "checks": checks,
            "join_health": join_health,
            "client_log_evidence": client_log_evidence,
            "hook_context": hook_status,
            "write_probe": probe_payload,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        if failed:
            raise typer.Exit(1)
        return

    console.print("agentacct MCP doctor")
    console.print(f"Tools: {', '.join(tool_names)}")
    for check in checks:
        if check["name"] == "mcp tools":
            continue
        console.print(f"{check['status']}: {check['name']} — {check['details']}")
    if join_health is not None:
        console.print("Join health:")
        console.print(f"  usage truth rows: {join_health['usage_rows']} ({join_health['usage_rows_with_join_keys']} with join keys)")
        console.print(f"  client_context events: {join_health['context_events']} ({join_health['context_events_with_join_keys']} joinable)")
        console.print(f"  section events: {join_health['section_events']} ({join_health['section_events_with_join_keys']} joinable)")
    if client_log_evidence is not None:
        console.print(
            f"  client-log evidence: {client_log_evidence['donor_usage_rows']} usage row(s) carry "
            f"{client_log_evidence['evidenced_event_ids_total']} evidenced event id(s); "
            f"{client_log_evidence['evidenced_events_in_store']} recorded event(s) in this store are session-linked "
            f"({client_log_evidence['outputs_skipped']} unpaired output(s) skipped)"
        )
    if hook_status is not None:
        if hook_status["status"] == "fresh":
            session_label = str(hook_status["client_session_id"] or "")[:8]
            fresh_count = int(hook_status.get("fresh_count") or 1)
            if fresh_count > 1:
                console.print(
                    f"  claude-code hook context: fresh ({fresh_count} concurrent session contexts; sections refuse id inheritance unless the server can bind to its own session)"
                )
            else:
                console.print(f"  claude-code hook context: fresh (age {int(hook_status['age_seconds'] or 0)}s, session {session_label}…, sections inherit joinable client-derived ids)")
        elif hook_status["status"] == "stale":
            console.print(f"  claude-code hook context: stale (age {int(hook_status['age_seconds'] or 0)}s); sections will not inherit it until the hook fires again")
        elif hook_status["status"] == "invalid":
            console.print("  claude-code hook context: invalid (file exists but is not usable)")
        else:
            console.print("  claude-code hook context: absent (install with: agentacct hooks claude-code install)")
    if join_health is not None:
        for warning in join_health["warnings"]:
            console.print(f"Warning: {warning}")
    console.print("Read-only diagnostics: no events were written. Use --write-probe to test the write path against a throwaway temp store.")
    if failed:
        raise typer.Exit(1)


@mcp_app.command("workflow-smoke")
def mcp_workflow_smoke(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help="State directory for the smoke events. Defaults to a THROWAWAY temporary store — this smoke records real events and must never land in a production ledger by default."),
    ] = None,
    run_id: Annotated[str, typer.Option(help="Safe run_id to associate with the workflow-smoke event.")] = "mcp_workflow_smoke",
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Exercise a full local MCP event workflow: initialize, tool list, record, list, summary."""
    # Same scratch rule as `demo`: explicit flag and AGENTACCT_STORE_DIR
    # are honored, otherwise the smoke's real events land in a throwaway temp
    # store — never a silently-resolved production ledger.
    resolved_store_dir, store_is_temporary = _resolve_scratch_store_dir(store_dir, label="workflow-smoke")
    try:
        payload = run_mcp_event_workflow_smoke(store_dir=resolved_store_dir, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - CLI should show concise workflow-smoke failures.
        raise typer.BadParameter(str(exc)) from exc
    payload["store_is_temporary"] = store_is_temporary
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    status = "ok" if payload.get("ok") else "failed"
    console.print(f"agentacct MCP workflow smoke: {status}")
    if payload.get("store_is_temporary"):
        console.print(f"Store: {payload.get('store_dir')} (throwaway temporary store; vanishes on cleanup/reboot)")
    else:
        console.print(f"Store: {payload.get('store_dir')}")
    console.print(f"Run ID: {payload.get('run_id')}")
    console.print(f"Event ID: {payload.get('event_id')}")
    console.print(f"Event round-tripped: {payload.get('event_round_tripped')}")
    console.print(f"Summary ok: {payload.get('summary_ok')}")
    console.print(f"Metadata redacted: {payload.get('metadata_redacted')}")
    if not payload.get("ok"):
        raise typer.Exit(1)


@event_app.command("record")
def event_record(
    source: Annotated[str, typer.Option(help="Integration source, e.g. codex, claude-code, custom-agent.")],
    event_type: Annotated[str, typer.Option(help="Event type, e.g. task_note, model_usage, checkpoint.")],
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    run_id: Annotated[Optional[str], typer.Option(help="Optional agentacct run ID to associate with this event.")] = None,
    provider: Annotated[Optional[str], typer.Option(help="Optional model/API provider label.")] = None,
    model: Annotated[Optional[str], typer.Option(help="Optional model label.")] = None,
    estimated_input_tokens: Annotated[Optional[int], typer.Option(help="Optional non-negative input token estimate.")] = None,
    estimated_output_tokens: Annotated[Optional[int], typer.Option(help="Optional non-negative output token estimate.")] = None,
    estimated_cost_usd: Annotated[Optional[float], typer.Option(help="Optional non-negative estimated cost in USD.")] = None,
    usage_confidence: Annotated[Optional[str], typer.Option(help="Optional usage confidence label.")] = None,
    cost_confidence: Annotated[Optional[str], typer.Option(help="Optional cost confidence label.")] = None,
    metadata_json: Annotated[Optional[str], typer.Option(help="Optional JSON object with event metadata. Secrets are redacted before storage.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Record a local integration event for the dashboard/event hub without paid API calls."""
    source = _limited_text(source, field="source", max_length=80) or ""
    event_type = _limited_text(event_type, field="event_type", max_length=80) or ""
    if not source.strip() or not event_type.strip():
        raise typer.BadParameter("--source and --event-type must be non-empty")
    run_id = _validated_optional_run_id(run_id)
    event = {
        "source": source,
        "event_type": event_type,
        "run_id": run_id,
        "provider": _limited_text(provider, field="provider", max_length=80),
        "model": _limited_text(model, field="model", max_length=120),
        "estimated_input_tokens": _optional_non_negative_int(estimated_input_tokens, field="estimated_input_tokens"),
        "estimated_output_tokens": _optional_non_negative_int(estimated_output_tokens, field="estimated_output_tokens"),
        "estimated_cost_usd": _optional_non_negative_float(estimated_cost_usd, field="estimated_cost_usd"),
        "usage_confidence": _limited_text(usage_confidence, field="usage_confidence", max_length=80),
        "cost_confidence": _limited_text(cost_confidence, field="cost_confidence", max_length=80),
        "metadata": _parse_metadata_json(metadata_json),
    }
    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    recorded = SentinelService(resolved_store_dir).record_event(event, transport="cli")
    if json_output:
        print(json.dumps({"event": recorded}, indent=2, sort_keys=True))
        return
    print(f"Recorded event {recorded['event_id']}: {recorded['source']} {recorded['event_type']}")
    print(f"Local API: agentacct serve --store-dir {shlex.quote(str(resolved_store_dir))}")


@event_app.command("note")
def event_note(
    summary: Annotated[str, typer.Argument(help="Plain-language note to record in the local event ledger.")],
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    source: Annotated[str, typer.Option(help="Integration source label for this note.")] = "manual",
    run_id: Annotated[Optional[str], typer.Option(help="Optional agentacct run ID to associate with this note.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Record a plain note without hand-writing metadata JSON."""
    source = _limited_text(source, field="source", max_length=80) or ""
    summary = _limited_text(summary, field="summary", max_length=1000) or ""
    if not source.strip() or not summary.strip():
        raise typer.BadParameter("source and summary must be non-empty")
    run_id = _validated_optional_run_id(run_id)
    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    recorded = SentinelService(resolved_store_dir).record_event(
        {
            "source": source,
            "event_type": "note",
            "run_id": run_id,
            "provider": None,
            "model": None,
            "estimated_input_tokens": None,
            "estimated_output_tokens": None,
            "estimated_cost_usd": None,
            "usage_confidence": None,
            "cost_confidence": None,
            "metadata": {"summary": summary},
        },
        transport="cli",
    )
    if json_output:
        print(json.dumps({"event": recorded}, indent=2, sort_keys=True))
        return
    display_summary = _metadata_summary(recorded.get("metadata")) or "[no summary]"
    print(f"Recorded note {recorded['event_id']}: {display_summary}")
    print(f"Local API: agentacct serve --store-dir {shlex.quote(str(resolved_store_dir))}")


@event_app.command("summary")
def event_summary(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    limit: Annotated[int, typer.Option(help="Number of recent events to summarize.")] = 200,
    run_id: Annotated[Optional[str], typer.Option(help="Optional run ID filter.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Show a compact local event-ledger summary."""
    if limit < 1 or limit > 200:
        raise typer.BadParameter("--limit must be between 1 and 200")
    run_id = _validated_optional_run_id(run_id)
    summary = SentinelService(_resolve_cli_store_dir(store_dir).path, create=False).summarize_events(limit=limit, run_id=run_id)
    if json_output:
        print(json.dumps({"summary": summary}, indent=2, sort_keys=True))
        return
    if not summary["event_count"]:
        print(_EMPTY_EVENT_LEDGER_HINT)
    print("agentacct event summary")
    if run_id:
        print(f"Run ID: {run_id}")
    print(f"Events summarized: {summary['event_count']} of latest {summary['limit']}")
    print(f"Notes: {summary['note_count']}")
    print(f"Estimated cost: {_safe_usd(summary['estimated_cost_usd'])}")
    print(
        "Estimated tokens: "
        f"input={summary['estimated_input_tokens']} "
        f"output={summary['estimated_output_tokens']} "
        f"total={summary.get('estimated_total_tokens', 0)} "
        f"cache_create={summary.get('cache_creation_input_tokens', 0)} "
        f"cache_read={summary.get('cache_read_input_tokens', 0)} "
        f"cached_input={summary.get('cached_input_tokens', 0)} "
        f"reasoning_output={summary.get('reasoning_output_tokens', 0)} "
        f"total_including_cached={summary.get('total_tokens_including_cached', 0)}"
    )
    _print_counter("By source", summary.get("by_source"))
    _print_counter("By type", summary.get("by_type"))
    _print_counter("By provider", summary.get("by_provider"))
    _print_counter("By usage confidence", summary.get("by_usage_confidence"))
    _print_counter("By cost confidence", summary.get("by_cost_confidence"))
    bridge = summary.get("usage_context_bridge")
    if isinstance(bridge, dict):
        print(
            "Usage context bridge: "
            f"usage_records={bridge.get('usage_records', 0)} "
            f"context_matched_usage_records={bridge.get('context_matched_usage_records', 0)} "
            f"attributed_usage_records={bridge.get('attributed_usage_records', bridge.get('linked_usage_records', 0))} "
            f"context_events={bridge.get('context_events', 0)} "
            f"unlinked_context_events={bridge.get('unlinked_context_events', 0)}"
        )
    tokens_by_provider = summary.get("tokens_by_provider")
    if isinstance(tokens_by_provider, dict) and tokens_by_provider:
        print("Provider token usage:")
        for provider, stats in sorted(tokens_by_provider.items()):
            if not isinstance(stats, dict):
                continue
            print(
                "  "
                f"{provider}: "
                f"events={stats.get('event_count', 0)} "
                f"input={stats.get('estimated_input_tokens', 0)} "
                f"output={stats.get('estimated_output_tokens', 0)} "
                f"total={stats.get('estimated_total_tokens', 0)} "
                f"cache_create={stats.get('cache_creation_input_tokens', 0)} "
                f"cache_read={stats.get('cache_read_input_tokens', 0)} "
                f"cached_input={stats.get('cached_input_tokens', 0)} "
                f"reasoning_output={stats.get('reasoning_output_tokens', 0)} "
                f"total_including_cached={stats.get('total_tokens_including_cached', 0)} "
                f"estimated_cost={_safe_usd(stats.get('estimated_cost_usd'))}"
            )


@event_app.command("verify-log")
def event_verify_log(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Prove the SQLite event log matches events.jsonl, line for line.

    The migration's step-3 gate: run this (in the default mirror mode) before
    cutting the store over to SQLite. In authoritative mode it just reports that
    the log is the authority.
    """

    service = SentinelService(_resolve_cli_store_dir(store_dir).path, create=False)
    result = service.verify_event_log_parity()
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if not result.get("available"):
        print(f"event log unavailable: {result.get('detail')}", file=sys.stderr)
        raise typer.Exit(2)
    if result.get("authoritative"):
        print(f"SQLite log is authoritative: {result.get('log_lines')} events (events.jsonl bypassed)")
        return
    if result.get("matches"):
        print(f"parity OK: SQLite log == events.jsonl ({result.get('log_lines')} lines)")
    else:
        print(
            f"PARITY MISMATCH: {result.get('detail')} "
            f"(file={result.get('file_lines')}, log={result.get('log_lines')})",
            file=sys.stderr,
        )
        raise typer.Exit(2)


@event_app.command("drop-flat-ledger")
def event_drop_flat_ledger(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    confirm: Annotated[bool, typer.Option("--confirm", help="Actually delete (irreversible).")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Cut the store over to the SQLite log and delete events.jsonl.

    Performs the cutover atomically under the events write lock: a final sync +
    line-for-line parity proof, then a PERSISTENT authoritative marker is
    written (so every future open reads the log, no env var needed) and
    events.jsonl is deleted. Refuses if the log is unavailable or parity does
    not hold. Irreversible; the SQLite log becomes the sole ledger.
    """

    resolved = _resolve_cli_store_dir(store_dir).path
    service = SentinelService(resolved, create=False)
    if service.event_log is None:
        print("refused: the SQLite event log is unavailable.", file=sys.stderr)
        raise typer.Exit(2)
    events_path = resolved / "events.jsonl"
    # The whole cutover holds the write lock so no concurrent append/rewrite can
    # slip between the parity proof and the deletion. The lock FILE is kept.
    with service._events_write_lock():
        already_authoritative = service._authoritative()
        if not already_authoritative and events_path.exists():
            # Mirror-mode cutover: the flat file is the authority, so sync the
            # log from it and prove line-for-line parity before it becomes the
            # ledger. (An ALREADY-authoritative store's log LEADS the frozen
            # file — reconciling would wipe the log's post-adoption writes, so
            # skip it; the file is a stale artifact safe to drop.)
            service.event_log.reconcile_from_file(events_path)
            result = service.event_log.verify_against_file(events_path)
            if not result.matches:
                print(
                    f"refused: SQLite log does not match events.jsonl ({result.detail}); "
                    "not deleting.",
                    file=sys.stderr,
                )
                raise typer.Exit(2)
        if not confirm:
            payload = {"would_delete": str(events_path), "log_events": service.event_log.count()}
            if json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"would cut over and delete {events_path} "
                    f"(SQLite log has {payload['log_events']} events). Re-run with --confirm."
                )
            return
        if not already_authoritative:
            service.mark_authoritative()
        elif events_path.exists():
            # Already authoritative: the log leads the frozen file. An old
            # mirror-mode process may have written a straggler to events.jsonl
            # between this service opening and this lock; drain it into the log
            # before deleting the file, so the cutover never discards an
            # un-absorbed event (the drain empties the file under this lock).
            service._absorb_flat_stragglers()
        events_path.unlink(missing_ok=True)
    payload = {"deleted": str(events_path), "log_events": service.event_log.count()}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"cut over: deleted {events_path}; the SQLite event log is now the sole ledger.")


@event_app.command("list")
def event_list(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    limit: Annotated[int, typer.Option(help="Number of recent events to show.")] = 20,
    run_id: Annotated[Optional[str], typer.Option(help="Optional run ID filter.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """List recent local integration events recorded for the dashboard/event hub."""
    if limit < 1 or limit > 200:
        raise typer.BadParameter("--limit must be between 1 and 200")
    run_id = _validated_optional_run_id(run_id)
    events = SentinelService(_resolve_cli_store_dir(store_dir).path, create=False).list_events(limit=limit, run_id=run_id)
    if json_output:
        print(json.dumps({"events": events}, indent=2, sort_keys=True))
        return
    if not events:
        print("No events recorded.")
        print(_EMPTY_EVENT_LEDGER_HINT)
        return
    for event in events:
        print(
            " ".join(
                [
                    str(event.get("event_id") or ""),
                    f"created={event.get('created_at') or ''}",
                    f"source={event.get('source') or ''}",
                    f"type={event.get('event_type') or ''}",
                    f"run_id={event.get('run_id') or ''}",
                    f"provider={event.get('provider') or ''}",
                    f"model={event.get('model') or ''}",
                    f"estimated_cost={_safe_usd(event.get('estimated_cost_usd'))}",
                    f"summary={_metadata_summary(event.get('metadata'))}",
                ]
            )
        )


_EVIDENCE_REBUILD_RECEIPT_MAX_BYTES = 16 * 1024 * 1024


@evidence_app.command("status")
def evidence_status(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Show durable spool/projection health without reading client transcripts."""

    runtime = EvidenceRuntime(_resolve_cli_store_dir(store_dir).path)
    payload = runtime.status()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Evidence v2: {'enabled' if payload.get('enabled') else 'disabled'}")
    if not payload.get("enabled"):
        print(f"Reason: {payload.get('reason') or 'disabled'}")
        return
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    print(f"Logical events: {stats.get('logical_events', 0)}")
    print(f"Evidence versions: {stats.get('evidence_versions', 0)}")
    print(f"Receipts: {stats.get('receipts', 0)}")
    print(f"Duplicates: {stats.get('duplicate_receipts', 0)}")
    print(f"Conflict groups: {stats.get('conflict_groups', 0)}")
    print(f"Invalid spool records: {stats.get('invalid_spool_records', 0)}")
    print(f"Spool: {payload.get('spool_path')}")


@evidence_app.command("list")
def evidence_list(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    limit: Annotated[int, typer.Option(help="Number of indexed evidence versions to show (1-1000).")] = 50,
    source_type: Annotated[Optional[str], typer.Option(help="Optional exact source-type filter.")] = None,
    dimension: Annotated[Optional[str], typer.Option(help="Optional exact evidence-dimension filter.")] = None,
    assertion: Annotated[Optional[str], typer.Option(help="Optional observed or claimed filter.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """List Evidence v2 envelopes from the indexed projection, never the raw spool."""

    if limit < 1 or limit > 1000:
        raise typer.BadParameter("--limit must be between 1 and 1000")
    if assertion not in {None, "observed", "claimed"}:
        raise typer.BadParameter("--assertion must be observed or claimed")
    runtime = EvidenceRuntime(_resolve_cli_store_dir(store_dir).path)
    records = runtime.records(
        limit=limit,
        descending=True,
        source_type=source_type,
        dimension=dimension,
        assertion=assertion,
    )
    rows = [
        {
            "envelope": record.envelope.to_dict(),
            "first_receipt_sequence": record.first_receipt_sequence,
            "last_receipt_sequence": record.last_receipt_sequence,
            "receipt_count": record.receipt_count,
            "duplicate_receipt_count": record.duplicate_receipt_count,
            "is_conflict": record.is_conflict,
        }
        for record in records
    ]
    if json_output:
        print(json.dumps({"evidence": rows}, indent=2, sort_keys=True))
        return
    if not rows:
        print("No Evidence v2 records match the filters.")
        return
    for row in rows:
        envelope = row["envelope"]
        print(
            " ".join(
                [
                    str(envelope.get("evidence_id") or ""),
                    f"assertion={envelope.get('assertion') or ''}",
                    f"source={envelope.get('source_type') or ''}/{envelope.get('source_system') or ''}",
                    f"type={envelope.get('event_type') or ''}",
                    f"dimensions={','.join(envelope.get('dimensions') or [])}",
                    f"completeness={(envelope.get('completeness') or {}).get('status', 'unknown')}",
                    f"receipts={row['receipt_count']}",
                    f"conflict={str(row['is_conflict']).lower()}",
                ]
            )
        )


@evidence_app.command("replay-v1")
def evidence_replay_v1(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Idempotently shadow the existing v1 event ledger into Evidence v2."""

    resolved = _resolve_cli_store_dir(store_dir).path
    service = SentinelService(resolved, create=False)
    payload = service.evidence.replay_v1(service.list_all_events(), transport="internal")
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print("Evidence v1 replay")
    print(f"Input: {payload['input_count']}")
    print(f"Inserted: {payload['inserted_count']}")
    print(f"Duplicates: {payload['duplicate_count']}")
    print(f"Conflicts: {payload['conflict_count']}")
    print(f"Errors: {payload['error_count']}")


@evidence_app.command("product")
def evidence_product(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    limit: Annotated[int, typer.Option(help="Maximum evidence versions to project (1-10000).")] = 10_000,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Show the Work Graph, Matrix, Discrepancy, and Basis projection summary."""

    if limit < 1 or limit > 10_000:
        raise typer.BadParameter("--limit must be between 1 and 10000")
    payload = EvidenceRuntime(_resolve_cli_store_dir(store_dir).path).product(limit=limit)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    summary = payload["summary"]
    print("agentacct multi-source evidence")
    print(f"Evidence: {summary['evidence_count']}")
    print(f"Observed: {summary['observed_count']}")
    print(f"Claimed: {summary['claimed_count']}")
    print(f"Sources: {summary['source_count']}")
    print(f"Discrepancies: {summary['discrepancy_count']}")


@evidence_app.command("work-event")
def evidence_work_event(
    source: Annotated[str, typer.Option(help="Claiming integration or agent source.")],
    kind: Annotated[str, typer.Option(help="Work Event kind: task, section, machine_check, client_context, usage_debug, note, or event.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    status: Annotated[str, typer.Option(help="started, checkpoint, completed, blocked, passed, failed, or unknown.")] = "unknown",
    occurred_at: Annotated[Optional[float], typer.Option(help="Optional source occurrence time as Unix seconds; distinct from local receipt time.")] = None,
    source_event_id: Annotated[Optional[str], typer.Option(help="Stable source event identifier used to make retries idempotent.")] = None,
    run_id: Annotated[Optional[str], typer.Option(help="Optional agentacct run identifier.")] = None,
    work_id: Annotated[Optional[str], typer.Option(help="Optional external work-item identifier.")] = None,
    section_id: Annotated[Optional[str], typer.Option(help="Optional semantic section identifier.")] = None,
    client: Annotated[Optional[str], typer.Option(help="Optional client label.")] = None,
    client_session_id: Annotated[Optional[str], typer.Option(help="Optional exact client session identifier.")] = None,
    title: Annotated[Optional[str], typer.Option(help="Optional short title.")] = None,
    summary: Annotated[Optional[str], typer.Option(help="Optional human-readable claim; never usage/billing truth.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Record a transport-neutral semantic claim through the CLI transport."""

    if kind not in WORK_EVENT_KINDS:
        raise typer.BadParameter("--kind must be one of: " + ", ".join(sorted(WORK_EVENT_KINDS)))
    if status not in WORK_EVENT_STATUSES:
        raise typer.BadParameter("--status must be one of: " + ", ".join(sorted(WORK_EVENT_STATUSES)))
    occurred_at = _optional_non_negative_float(occurred_at, field="occurred_at")
    source_event_id = _limited_text(source_event_id, field="source_event_id", max_length=240)
    if source_event_id is not None:
        source_event_id = source_event_id.strip()
        if not source_event_id or any(character in source_event_id for character in ("\r", "\n", "\x00")):
            raise typer.BadParameter("--source-event-id must be non-blank and contain no control line breaks")
    event_type = {
        "section": f"section_{status}",
        "task": f"task_{status}",
        "machine_check": "machine_check",
        "client_context": "client_context_attached",
        "usage_debug": "agent_usage_debug_reported",
        "note": "note",
        "event": "work_event",
    }[kind]
    work_event = WorkEvent(
        event_kind=kind,
        source=_limited_text(source, field="source", max_length=80) or "",
        transport="cli",
        status=status,
        occurred_at=occurred_at if occurred_at is not None else time.time(),
        source_event_id=source_event_id,
        run_id=_validated_optional_run_id(run_id),
        work_id=work_id,
        section_id=section_id,
        title=title,
        summary=summary,
        client=client,
        client_session_id=client_session_id,
        original_event_type=event_type,
    )
    recorded = SentinelService(_resolve_cli_store_dir(store_dir).path).record_event(
        work_event.to_v1_event(),
        transport="cli",
    )
    payload = {"work_event": WorkEvent.from_v1_event(recorded, transport="cli").to_dict(), "v1_event": recorded}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Recorded Work Event {recorded['event_id']}: {kind} {status}")


@capture_app.command("capabilities")
def capture_capabilities(
    vendor: Annotated[Optional[str], typer.Option(help="Optional claude-code, codex, or cursor filter.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Describe exactly which host events and evidence dimensions can be observed."""

    if vendor is not None:
        capabilities = DEFAULT_CAPTURE_REGISTRY.capabilities(vendor)
        if capabilities is None or isinstance(capabilities, dict):
            raise typer.BadParameter(f"unsupported capture vendor: {vendor}")
        payload = {"capabilities": {capabilities.vendor: capabilities.to_dict()}}
    else:
        all_capabilities = DEFAULT_CAPTURE_REGISTRY.capabilities()
        assert isinstance(all_capabilities, dict)
        payload = {
            "capabilities": {
                name: capability.to_dict()
                for name, capability in all_capabilities.items()
            }
        }
    payload["privacy"] = {
        "capture_mode": "metadata_only",
        "captures_usage": False,
        "captures_cost": False,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for name, capability in payload["capabilities"].items():
        print(
            f"{name}: events={','.join(capability['supported_host_events'])} "
            f"identity={capability['stable_event_identity']} "
            f"usage={str(capability['usage']).lower()} cost={str(capability['cost']).lower()}"
        )


@capabilities_app.command("agents")
def agent_capabilities_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit the complete machine-readable manifest.")] = False,
) -> None:
    """Show independent capability lanes without a whole-client support badge."""

    payload = agent_capability_manifest()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print("Agent capability coverage")
    state_labels = {
        "verified": "verified",
        "verified_partial": "limited",
        "experimental": "experimental",
        "unavailable": "unavailable",
    }
    capability_labels = {
        "session_discovery": "Sessions",
        "usage_import": "Usage",
        "mechanical_capture": "Mechanical",
        "mcp_semantics": "MCP semantics",
        "model_attribution": "Model",
        "cache_read": "Cache read",
        "cache_write": "Cache write",
        "automatic_install": "Automatic install",
    }
    for row in payload["clients"]:
        capabilities = row["capabilities"]
        print(f"\n{row['display_name']}")
        for name, capability in capabilities.items():
            verification = capability["verification"]
            verified = verification["level"]
            if verification.get("verified_at"):
                verified += f" · {verification['verified_at']}"
            print(
                f"  {capability_labels[name]}: {state_labels[capability['state']]} "
                f"| {capability['activation']} | {verified}"
            )
    print("\nRuntime source detection and ingestion health are separate from this static manifest.")


@capture_app.command("manifest")
def capture_manifest(
    vendor: Annotated[str, typer.Option(help="claude-code, codex, or cursor.")],
    command: Annotated[str, typer.Option(help="Single-line installed command used in the rendered fragment.")] = "agentacct capture hook",
    content_only: Annotated[bool, typer.Option("--content-only", help="Print only the host configuration fragment.")] = False,
) -> None:
    """Render an opt-in hook fragment without reading or writing host configuration."""

    try:
        rendered = render_hook_manifest(vendor, command=command)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload: dict[str, Any]
    if content_only:
        payload = rendered.to_dict()
    else:
        payload = {
            "vendor": rendered.vendor,
            "relative_path": rendered.relative_path,
            "content": rendered.to_dict(),
            "written": False,
            "activation": "opt_in",
        }
    print(json.dumps(payload, indent=2, sort_keys=True))


@capture_app.command("hook")
def capture_hook(
    vendor: Annotated[str, typer.Option(help="Host vendor supplied by a rendered hook manifest.")],
    event: Annotated[str, typer.Option(help="Host hook event supplied by the manifest.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print a metadata-only diagnostic receipt.")] = False,
) -> None:
    """Read one bounded JSON object from stdin and always return fail-open."""

    payload: dict[str, Any]
    try:
        raw = sys.stdin.buffer.read(DEFAULT_MAX_PAYLOAD_BYTES + 1)
        runtime = EvidenceRuntime(_resolve_cli_store_dir(store_dir).path)
        environment = {
            key: value
            for key in (
                "CURSOR_AGENT",
                "CURSOR_SESSION_ID",
                "CURSOR_TRACE_ID",
                "CURSOR_PROJECT_PATH",
                "CURSOR_VERSION",
                "CLAUDECODE",
            )
            if (value := os.environ.get(key))
        }
        payload = capture_hook_payload(
            runtime,
            vendor=vendor,
            host_event=event,
            payload=raw,
            context=CaptureContext(
                host_event=event,
                environment=environment,
                workspace_roots=(str(Path.cwd()),),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - the coding-agent hook must continue.
        payload = {
            "schema_version": "agent-chronicle.capture-receipt.v1",
            "enabled": False,
            "vendor": vendor.strip().lower(),
            "host_event": event.strip(),
            "ok": False,
            "fail_open": True,
            "attempted_count": 0,
            "stored_count": 0,
            "ignored_reason": "hook_entrypoint_failed",
            "warnings": [f"entrypoint_error:{type(exc).__name__}"],
            "write_errors": ["hook_entrypoint_failed"],
            "observations": [],
            "privacy": {
                "capture_mode": "metadata_only",
                "raw_payload_returned": False,
                "hook_exit_policy": "always_zero",
            },
        }
    # Installed hooks stay silent: stdout can be interpreted as agent context
    # or control JSON by some hosts. Diagnostics are explicit and body-free.
    if json_output:
        print(json.dumps(payload, sort_keys=True))


def _emit_connector_result(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Connector: {payload['connector']}")
        print(f"Records: {payload['record_count']}")
        print(f"Inserted: {payload['inserted_count']}")
        print(f"Duplicates: {payload['duplicate_count']}")
        print(f"Conflicts: {payload['conflict_count']}")
        print(f"Errors: {payload['error_count']}")
        print(f"Dry run: {str(payload['dry_run']).lower()}")
    if int(payload.get("error_count") or 0) > 0:
        raise typer.Exit(1)


@connector_app.command("list")
def connector_list(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """List connector capabilities and their upstream mutation boundary."""

    payload = {
        "connectors": [
            {"name": "paperclip", "input": "exported JSON snapshot", "upstream_access": "read_only"},
            {"name": "openlit", "input": "OTLP HTTP JSON", "upstream_access": "read_only"},
            {"name": "entire", "input": "public Git refs and metadata", "upstream_access": "read_only"},
        ],
        "external_writes": False,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print("Read-only evidence connectors")
    for connector in payload["connectors"]:
        print(f"- {connector['name']}: {connector['input']} ({connector['upstream_access']})")


@connector_app.command("paperclip")
def connector_paperclip(
    snapshot: Annotated[Path, typer.Argument(help="Paperclip exported JSON snapshot.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Parse and normalize without writing agentacct evidence.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Import Paperclip control-plane records as orchestrator claims."""

    try:
        records = PaperclipSnapshotConnector().read(snapshot)
        result = import_connector_records(
            EvidenceRuntime(_resolve_cli_store_dir(store_dir).path),
            records,
            connector="paperclip",
            dry_run=dry_run,
        )
    except (ConnectorError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    _emit_connector_result(result.to_dict(), json_output=json_output)


@connector_app.command("openlit")
def connector_openlit(
    otlp_json: Annotated[Path, typer.Argument(help="OpenLIT/OpenTelemetry OTLP trace JSON export.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Parse and normalize without writing agentacct evidence.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Import metadata-only OpenLIT/OTLP spans into the durable v2 spool."""

    try:
        records = OpenLITOTLPConnector().read(otlp_json)
        result = import_connector_records(
            EvidenceRuntime(_resolve_cli_store_dir(store_dir).path),
            records,
            connector="openlit",
            dry_run=dry_run,
        )
    except (ConnectorError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    _emit_connector_result(result.to_dict(), json_output=json_output)


@connector_app.command("entire")
def connector_entire(
    repository: Annotated[Path, typer.Argument(help="Local Git repository containing Entire public checkpoint refs.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    max_commits: Annotated[int, typer.Option(help="Maximum commits read from known Entire refs (1-1000).")] = 100,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Read and normalize Git metadata without writing agentacct evidence.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Read Entire checkpoint metadata without mutating refs, index, or worktree."""

    try:
        records = EntireGitConnector(repository, max_commits=max_commits).read()
        result = import_connector_records(
            EvidenceRuntime(_resolve_cli_store_dir(store_dir).path),
            records,
            connector="entire",
            dry_run=dry_run,
        )
    except (ConnectorError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    _emit_connector_result(result.to_dict(), json_output=json_output)


def _strict_control_json(value: str, *, option: str, expected: type) -> Any:
    """Parse a bounded, standards-compliant JSON CLI value."""

    try:
        parsed = json.loads(
            value,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant is not allowed: {constant}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{option} must be valid JSON: {exc.msg}") from exc
    except ValueError as exc:
        raise typer.BadParameter(f"{option} must be strict JSON: {exc}") from exc
    if not isinstance(parsed, expected):
        expected_name = "array" if expected is list else "object"
        raise typer.BadParameter(f"{option} must decode to a JSON {expected_name}")
    try:
        encoded = json.dumps(parsed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(f"{option} must be strict JSON: {exc}") from exc
    if len(encoded) > 65_536:
        raise typer.BadParameter(f"{option} must be <= 65536 bytes when JSON encoded")
    return parsed


def _strict_control_argv(value: str) -> list[str]:
    parsed = _strict_control_json(value, option="--argv-json", expected=list)
    if not parsed:
        raise typer.BadParameter("--argv-json must contain at least one argument")
    if len(parsed) > 256:
        raise typer.BadParameter("--argv-json must contain at most 256 arguments")
    argv: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            raise typer.BadParameter("--argv-json entries must all be strings")
        if not item or any(character in item for character in ("\x00", "\r", "\n")):
            raise typer.BadParameter("--argv-json entries must be non-empty and contain no NUL or newlines")
        if len(item) > 4096:
            raise typer.BadParameter("--argv-json entries must be <= 4096 characters")
        argv.append(item)
    return argv


def _control_idempotency_key(action: str, override: str | None, payload: dict[str, Any]) -> str:
    if override is not None:
        key = override.strip()
        if not key:
            raise typer.BadParameter("--idempotency-key must not be empty")
        if len(key) > 240 or any(character in key for character in ("\x00", "\r", "\n")):
            raise typer.BadParameter("--idempotency-key must be <= 240 characters and contain no NUL or newlines")
        return key
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter("control inputs must be strict JSON values") from exc
    digest = hashlib.sha256(action.encode("utf-8") + b"\0" + encoded).hexdigest()
    return f"cli:{action}:{digest}"


def _control_child_key(parent_key: str, action: str) -> str:
    digest = hashlib.sha256(f"{parent_key}\0{action}".encode("utf-8")).hexdigest()
    return f"cli:{action}:{digest}"


def _control_store(store_dir: Path) -> tuple[Path, ControlStore]:
    path = store_dir.expanduser().resolve()
    return path, ControlStore(path)


@contextmanager
def _friendly_control_plane_errors() -> Iterator[None]:
    try:
        yield
    except (ControlPlaneError, SupervisorError, OSError) as exc:
        print(f"Control request failed: {exc}", file=sys.stderr)
        raise typer.Exit(1) from exc


def _sanitized_control_attempt(attempt: Any) -> dict[str, Any]:
    """Expose decision state, never process-control authority or nonce material."""

    return {
        "attempt_id": attempt.attempt_id,
        "task_id": attempt.task_id,
        "contract_revision": attempt.contract_revision,
        "agent_id": attempt.agent_id,
        "workspace_id": attempt.workspace_id,
        "execution_state": attempt.execution_state,
        "outcome_state": attempt.outcome_state,
        "control_state": attempt.control_state,
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "exit_code": attempt.exit_code,
        "revision": attempt.revision,
        "reason_available": attempt.reason is not None,
    }


def _sanitized_control_agent(agent: Any) -> dict[str, Any]:
    return {
        "agent_id": agent.agent_id,
        "adapter": agent.adapter,
        "execution_backend": agent.execution_backend,
        "command_registered": bool(agent.argv_template),
        "enabled": agent.enabled,
        "revision": agent.revision,
        "updated_at": agent.updated_at,
    }


def _sanitized_control_approval(approval: Any) -> dict[str, Any]:
    return {
        "approval_id": approval.approval_id,
        "task_id": approval.task_id,
        "attempt_id": approval.attempt_id,
        "kind": approval.kind,
        "requested_action": approval.requested_action,
        "state": approval.state,
        "expires_at": approval.expires_at,
        "decided_at": approval.decided_at,
        "consumed_at": approval.consumed_at,
        "revision": approval.revision,
    }


def _sanitized_control_workspace(workspace: Any) -> dict[str, Any]:
    return {
        "workspace_id": workspace.workspace_id,
        "enabled": workspace.enabled,
        "revision": workspace.revision,
        "updated_at": workspace.updated_at,
        "workspace_store_configured": workspace.store_dir is not None,
    }


def _sanitized_control_contract(contract: Any) -> dict[str, Any]:
    return {
        "task_id": contract.task_id,
        "revision": contract.revision,
        "workspace_id": contract.workspace_id,
        "budget_policy_ids": list(contract.budget_policy_ids),
        "success_check_count": len(contract.success_checks),
        "permission_envelope_configured": bool(contract.permission_envelope),
        "created_at": contract.created_at,
    }


def _sanitized_control_projection(projection: ControlProjection) -> dict[str, Any]:
    collections: dict[str, list[dict[str, Any]]] = {
        "tasks": [item.to_dict() for item in sorted(projection.tasks.values(), key=lambda item: item.task_id)],
        "contracts": [
            _sanitized_control_contract(item)
            for item in sorted(projection.contracts.values(), key=lambda item: item.task_id)
        ],
        "agents": [
            _sanitized_control_agent(item)
            for item in sorted(projection.agents.values(), key=lambda item: item.agent_id)
        ],
        "workspaces": [
            _sanitized_control_workspace(item)
            for item in sorted(projection.workspaces.values(), key=lambda item: item.workspace_id)
        ],
        "attempts": [
            _sanitized_control_attempt(item)
            for item in sorted(projection.attempts.values(), key=lambda item: item.attempt_id)
        ],
        "approvals": [
            _sanitized_control_approval(item)
            for item in sorted(projection.approvals.values(), key=lambda item: item.approval_id)
        ],
        "budget_policies": [
            item.to_dict()
            for item in sorted(projection.budget_policies.values(), key=lambda item: item.policy_id)
        ],
        "schedules": [
            item.to_dict() for item in sorted(projection.schedules.values(), key=lambda item: item.schedule_id)
        ],
        "issues": [
            {
                "line_number": issue.line_number,
                "code": issue.code,
                "message_available": bool(issue.message),
                "event_id": issue.event_id,
            }
            for issue in projection.issues
        ],
    }
    return {
        "schema_version": "agent-chronicle.control-cli-projection.v1",
        "event_count": len(projection.events),
        "counts": {name: len(rows) for name, rows in collections.items()},
        **collections,
    }


def _emit_control_payload(payload: dict[str, Any], *, json_output: bool) -> None:
    rendered = dict(payload)
    key = rendered.pop("idempotency_key", None)
    if isinstance(key, str):
        rendered["idempotency"] = {
            "key_fingerprint": "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "replay": "repeat the same inputs or reuse the same explicit key",
        }
    if json_output:
        print(json.dumps(rendered, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        print(json.dumps(rendered, indent=2, sort_keys=True, allow_nan=False))


def _require_control_revision(
    projection: ControlProjection,
    *,
    collection: str,
    identifier: str,
    expected_revision: int,
    idempotency_key: str | None = None,
    idempotent_action: str | None = None,
    idempotent_next_state: str | None = None,
) -> None:
    if expected_revision < 0:
        raise typer.BadParameter("--expected-revision must be >= 0")
    records = getattr(projection, collection)
    record = records.get(identifier)
    if record is None:
        raise ControlPlaneError(f"{collection.rstrip('s').replace('_', ' ')} does not exist")
    previous = projection.idempotency.get(idempotency_key or "")
    if previous is not None:
        if (
            previous.target_id == identifier
            and (idempotent_action is None or previous.action == idempotent_action)
            and (idempotent_next_state is None or previous.next_state == idempotent_next_state)
        ):
            return
        raise ControlPlaneError("idempotency key belongs to a different control operation")
    if record.revision == expected_revision:
        return
    raise RevisionConflict(f"{identifier} revision is {record.revision}, expected {expected_revision}")


@control_app.command("status")
def control_status(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Show the sanitized agentacct-owned control projection."""

    _path, store = _control_store(store_dir)
    with _friendly_control_plane_errors():
        _emit_control_payload(_sanitized_control_projection(store.project()), json_output=json_output)


@control_app.command("list")
def control_list(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    kind: Annotated[
        str,
        typer.Option(
            help=(
                "Projection collection: tasks, contracts, agents, workspaces, attempts, approvals, "
                "budget-policies, schedules, or issues."
            )
        ),
    ] = "attempts",
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """List one sanitized control projection collection."""

    normalized = kind.strip().lower().replace("-", "_")
    allowed = {
        "tasks",
        "contracts",
        "agents",
        "workspaces",
        "attempts",
        "approvals",
        "budget_policies",
        "schedules",
        "issues",
    }
    if normalized not in allowed:
        raise typer.BadParameter(
            "--kind must be one of: " + ", ".join(sorted(name.replace("_", "-") for name in allowed))
        )
    _path, store = _control_store(store_dir)
    with _friendly_control_plane_errors():
        projection = _sanitized_control_projection(store.project())
        _emit_control_payload(
            {
                "schema_version": projection["schema_version"],
                "kind": normalized,
                "count": projection["counts"][normalized],
                "items": projection[normalized],
            },
            json_output=json_output,
        )


@control_app.command("register-workspace")
def control_register_workspace(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    root: Annotated[Path, typer.Option(help="Existing workspace root agentacct may launch inside.")],
    workspace_id: Annotated[Optional[str], typer.Option(help="Stable workspace id; derived when omitted.")] = None,
    expected_revision: Annotated[int, typer.Option(help="Expected current workspace revision (0 to create).")] = 0,
    enabled: Annotated[
        bool,
        typer.Option("--enabled/--disabled", help="Whether new attempts may use this workspace."),
    ] = True,
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable retry key; deterministically derived when omitted."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Register or revision-update one local workspace identity."""

    if expected_revision < 0:
        raise typer.BadParameter("--expected-revision must be >= 0")
    resolved_store, store = _control_store(store_dir)
    try:
        canonical_root = root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise typer.BadParameter("--root must be an existing directory") from exc
    payload = {
        "root": str(canonical_root),
        "workspace_id": workspace_id,
        "expected_revision": expected_revision,
        "enabled": enabled,
    }
    key = _control_idempotency_key("register-workspace", idempotency_key, payload)
    with _friendly_control_plane_errors():
        record = store.register_workspace(
            canonical_root,
            workspace_id=workspace_id,
            store_dir=resolved_store,
            enabled=enabled,
            expected_revision=expected_revision,
            idempotency_key=key,
        )
        _emit_control_payload(
            {"workspace": _sanitized_control_workspace(record), "idempotency_key": key},
            json_output=json_output,
        )


@control_app.command("register-agent")
def control_register_agent(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    agent_id: Annotated[str, typer.Option(help="Stable local agent registration id.")],
    display_name: Annotated[str, typer.Option(help="Human-readable agent name.")],
    argv_json: Annotated[str, typer.Option(help="Strict JSON argv array; shell strings are never accepted.")],
    expected_revision: Annotated[int, typer.Option(help="Expected current agent revision (0 to create).")] = 0,
    adapter: Annotated[str, typer.Option(help="Must be local_argv for the owned supervisor.")] = "local_argv",
    execution_backend: Annotated[str, typer.Option(help="Must be subprocess for the owned supervisor.")] = "subprocess",
    enabled: Annotated[
        bool,
        typer.Option("--enabled/--disabled", help="Whether new attempts may use this agent."),
    ] = True,
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable retry key; deterministically derived when omitted."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Register a fixed argv template for agentacct-owned launches only."""

    if expected_revision < 0:
        raise typer.BadParameter("--expected-revision must be >= 0")
    if adapter != "local_argv" or execution_backend != "subprocess":
        raise typer.BadParameter(
            "the local control CLI only registers adapter=local_argv with execution-backend=subprocess"
        )
    argv = _strict_control_argv(argv_json)
    _path, store = _control_store(store_dir)
    payload = {
        "agent_id": agent_id,
        "display_name": display_name,
        "argv": argv,
        "adapter": adapter,
        "execution_backend": execution_backend,
        "expected_revision": expected_revision,
        "enabled": enabled,
    }
    key = _control_idempotency_key("register-agent", idempotency_key, payload)
    with _friendly_control_plane_errors():
        record = store.register_agent(
            agent_id,
            display_name=display_name,
            adapter=adapter,
            execution_backend=execution_backend,
            argv_template=argv,
            capabilities=(),
            enabled=enabled,
            expected_revision=expected_revision,
            idempotency_key=key,
        )
        _emit_control_payload(
            {"agent": _sanitized_control_agent(record), "idempotency_key": key},
            json_output=json_output,
        )


@control_app.command("create-task")
@control_app.command("plan")
def control_plan_task(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    objective: Annotated[str, typer.Option(help="Task Contract objective.")],
    workspace_id: Annotated[str, typer.Option(help="Registered agentacct workspace id.")],
    agent_id: Annotated[str, typer.Option(help="Registered agentacct-owned agent id.")],
    permission_envelope_json: Annotated[
        str,
        typer.Option(help="Strict JSON object describing the allowed mutation envelope."),
    ] = "{}",
    budget_policy_id: Annotated[
        Optional[list[str]],
        typer.Option("--budget-policy-id", help="Budget policy id; repeatable."),
    ] = None,
    success_check: Annotated[
        Optional[list[str]],
        typer.Option("--success-check", help="Task success check; repeatable."),
    ] = None,
    expected_contract_revision: Annotated[
        int,
        typer.Option(help="Expected current Task Contract revision (0 for a new task)."),
    ] = 0,
    task_id: Annotated[Optional[str], typer.Option(help="Explicit new task id; derived when omitted.")] = None,
    attempt_id: Annotated[
        Optional[str],
        typer.Option(help="Explicit new attempt id; derived when omitted."),
    ] = None,
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable batch retry key; deterministically derived when omitted."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Create a planned Task, its contract, and one pending owned attempt."""

    if expected_contract_revision != 0:
        raise typer.BadParameter("--expected-contract-revision must be 0 because this command creates a new task")
    permission_envelope = _strict_control_json(
        permission_envelope_json,
        option="--permission-envelope-json",
        expected=dict,
    )
    policies = list(dict.fromkeys(budget_policy_id or ()))
    checks = list(dict.fromkeys(success_check or ()))
    _path, store = _control_store(store_dir)
    payload = {
        "objective": objective,
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "permission_envelope": permission_envelope,
        "budget_policy_ids": policies,
        "success_checks": checks,
        "expected_contract_revision": expected_contract_revision,
        "task_id": task_id,
        "attempt_id": attempt_id,
    }
    key = _control_idempotency_key("plan-task", idempotency_key, payload)
    with _friendly_control_plane_errors():
        projection = store.project()
        workspace = projection.workspaces.get(workspace_id)
        if workspace is None or not workspace.enabled:
            raise ControlPlaneError("planned task requires an enabled registered workspace")
        agent = projection.agents.get(agent_id)
        if agent is None or not agent.enabled:
            raise ControlPlaneError("planned task requires an enabled registered agent")
        if agent.adapter != "local_argv" or agent.execution_backend != "subprocess":
            raise ControlPlaneError("external or observed-only agents cannot receive planned control attempts")
        missing_policies = [policy_id for policy_id in policies if policy_id not in projection.budget_policies]
        if missing_policies:
            raise ControlPlaneError("planned task references an unknown budget policy")
        task = store.create_task(
            origin="planned",
            task_id=task_id,
            idempotency_key=_control_child_key(key, "create-task"),
        )
        contract = store.create_contract(
            task.task_id,
            objective=objective,
            workspace_id=workspace_id,
            permission_envelope=permission_envelope,
            budget_policy_ids=policies,
            success_checks=checks,
            expected_revision=expected_contract_revision,
            idempotency_key=_control_child_key(key, "create-contract"),
        )
        approval_required = contract_requires_launch_approval(contract.permission_envelope)
        attempt = store.create_attempt(
            task.task_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            contract_revision=contract.revision,
            attempt_id=attempt_id,
            initial_control_state="awaiting_approval" if approval_required else "ready",
            idempotency_key=_control_child_key(key, "create-attempt"),
        )
        _emit_control_payload(
            {
                "task": task.to_dict(),
                "contract": _sanitized_control_contract(contract),
                "attempt": _sanitized_control_attempt(attempt),
                "launch_approval_required": approval_required,
                "next_step": (
                    "Request launch approval for this held attempt."
                    if approval_required
                    else "Launch this ready attempt when you want execution to begin."
                ),
                "idempotency_key": key,
            },
            json_output=json_output,
        )


@control_app.command("register-budget-policy")
def control_register_budget_policy(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    policy_id: Annotated[str, typer.Option(help="Stable budget policy id.")],
    scope: Annotated[str, typer.Option(help="Policy scope, for example task or attempt.")],
    metric: Annotated[str, typer.Option(help="Measured budget dimension, for example cost_usd.")],
    limit: Annotated[float, typer.Option(help="Positive policy limit.")],
    basis: Annotated[
        str,
        typer.Option(help="Evidence basis; hard actions require provider_billed or conservative_approved."),
    ],
    action: Annotated[str, typer.Option(help="Policy action, for example warn, cancel, or block.")],
    expected_revision: Annotated[int, typer.Option(help="Expected current policy revision (0 to create).")] = 0,
    enabled: Annotated[
        bool,
        typer.Option("--enabled/--disabled", help="Whether the policy is active."),
    ] = True,
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable retry key; deterministically derived when omitted."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Register or revision-update a budget policy."""

    if expected_revision < 0:
        raise typer.BadParameter("--expected-revision must be >= 0")
    if not math.isfinite(limit) or limit <= 0:
        raise typer.BadParameter("--limit must be finite and > 0")
    _path, store = _control_store(store_dir)
    payload = {
        "policy_id": policy_id,
        "scope": scope,
        "metric": metric,
        "limit": limit,
        "basis": basis,
        "action": action,
        "enabled": enabled,
        "expected_revision": expected_revision,
    }
    key = _control_idempotency_key("register-budget-policy", idempotency_key, payload)
    with _friendly_control_plane_errors():
        record = store.register_budget_policy(
            policy_id,
            scope=scope,
            metric=metric,
            limit=limit,
            basis=basis,
            action=action,
            enabled=enabled,
            expected_revision=expected_revision,
            idempotency_key=key,
        )
        _emit_control_payload(
            {"budget_policy": record.to_dict(), "idempotency_key": key},
            json_output=json_output,
        )


@control_app.command("register-schedule")
def control_register_schedule(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    task_id: Annotated[str, typer.Option(help="Planned task id to schedule.")],
    cadence: Annotated[str, typer.Option(help="one_shot or fixed.")],
    next_run_at: Annotated[float, typer.Option(help="Next run as a Unix timestamp.")],
    interval_seconds: Annotated[
        Optional[float],
        typer.Option(help="Required for fixed cadence and forbidden for one_shot."),
    ] = None,
    schedule_id: Annotated[Optional[str], typer.Option(help="Explicit schedule id; derived when omitted.")] = None,
    enabled: Annotated[
        bool,
        typer.Option("--enabled/--disabled", help="Whether the schedule can be claimed."),
    ] = True,
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable retry key; deterministically derived when omitted."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Register a one-shot or fixed schedule without launching it."""

    if cadence not in {"one_shot", "fixed"}:
        raise typer.BadParameter("--cadence must be one_shot or fixed")
    if not math.isfinite(next_run_at) or next_run_at < 0:
        raise typer.BadParameter("--next-run-at must be a finite non-negative Unix timestamp")
    if cadence == "one_shot" and interval_seconds is not None:
        raise typer.BadParameter("--interval-seconds is not allowed for one_shot cadence")
    if cadence == "fixed" and (
        interval_seconds is None or not math.isfinite(interval_seconds) or interval_seconds < 1
    ):
        raise typer.BadParameter("--interval-seconds must be finite and >= 1 for fixed cadence")
    _path, store = _control_store(store_dir)
    payload = {
        "task_id": task_id,
        "cadence": cadence,
        "next_run_at": next_run_at,
        "interval_seconds": interval_seconds,
        "schedule_id": schedule_id,
        "enabled": enabled,
    }
    key = _control_idempotency_key("register-schedule", idempotency_key, payload)
    with _friendly_control_plane_errors():
        record = store.register_schedule(
            task_id,
            cadence=cadence,
            next_run_at=next_run_at,
            interval_seconds=interval_seconds,
            schedule_id=schedule_id,
            enabled=enabled,
            idempotency_key=key,
        )
        _emit_control_payload(
            {"schedule": record.to_dict(), "idempotency_key": key},
            json_output=json_output,
        )


@control_app.command("request-approval")
def control_request_approval(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    task_id: Annotated[str, typer.Option(help="Task awaiting a local decision.")],
    attempt_id: Annotated[str, typer.Option(help="Owned attempt that must be held before approval is requested.")],
    expected_attempt_revision: Annotated[
        int,
        typer.Option(help="Expected current attempt revision before it is held."),
    ],
    kind: Annotated[str, typer.Option(help="Approval kind.")],
    requested_action: Annotated[str, typer.Option(help="Action that would consume this approval.")],
    expires_at: Annotated[float, typer.Option(help="Expiry as a future Unix timestamp.")],
    requested_by: Annotated[str, typer.Option(help="Policy or actor requesting the decision.")] = "local-policy",
    approval_id: Annotated[Optional[str], typer.Option(help="Explicit approval id; derived when omitted.")] = None,
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable retry key; deterministically derived when omitted."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Hold an owned attempt, then request one expiring local approval."""

    if not math.isfinite(expires_at) or expires_at <= time.time():
        raise typer.BadParameter("--expires-at must be a finite future Unix timestamp")
    if requested_action != "launch":
        raise typer.BadParameter("--requested-action must be launch for an attempt-scoped control approval")
    if expected_attempt_revision < 1:
        raise typer.BadParameter("--expected-attempt-revision must be >= 1")
    _path, store = _control_store(store_dir)
    payload = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "expected_attempt_revision": expected_attempt_revision,
        "kind": kind,
        "requested_action": requested_action,
        "requested_by": requested_by,
        "expires_at": expires_at,
        "approval_id": approval_id,
    }
    key = _control_idempotency_key("request-approval", idempotency_key, payload)
    with _friendly_control_plane_errors():
        projection = store.project()
        attempt = _require_owned_attempt_surface(projection, attempt_id)
        if attempt.task_id != task_id:
            raise ControlPlaneError("approval task and attempt do not match")
        hold_key = _control_child_key(key, "hold-attempt")
        if hold_key not in projection.idempotency:
            if attempt.revision != expected_attempt_revision:
                raise RevisionConflict(
                    f"{attempt_id} revision is {attempt.revision}, expected {expected_attempt_revision}"
                )
            if attempt.control_state not in {"ready", "awaiting_approval"}:
                raise ControlPlaneError("attempt must be ready or awaiting approval before a launch request")
        held = store.transition_attempt(
            attempt_id,
            expected_revision=expected_attempt_revision,
            control_state="awaiting_approval",
            reason="local approval required",
            idempotency_key=hold_key,
            actor_kind="local_user",
            actor_id="local-user",
        )
        record = store.request_approval(
            task_id,
            attempt_id=attempt_id,
            kind=kind,
            requested_action=requested_action,
            requested_by=requested_by,
            expires_at=expires_at,
            approval_id=approval_id,
            idempotency_key=_control_child_key(key, "request-approval"),
        )
        _emit_control_payload(
            {
                "approval": _sanitized_control_approval(record),
                "attempt": _sanitized_control_attempt(held),
                "idempotency_key": key,
            },
            json_output=json_output,
        )


@control_app.command("decide-approval")
def control_decide_approval(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    approval_id: Annotated[str, typer.Option(help="Approval id to decide.")],
    decision: Annotated[str, typer.Option(help="approve or reject.")],
    expected_revision: Annotated[int, typer.Option(help="Expected current approval revision.")],
    decided_by: Annotated[str, typer.Option(help="Local actor recording the decision.")] = "local-user",
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable retry key; deterministically derived when omitted."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Approve or reject an expiring decision with optimistic concurrency."""

    normalized = decision.strip().lower()
    if normalized not in {"approve", "reject"}:
        raise typer.BadParameter("--decision must be approve or reject")
    if expected_revision < 1:
        raise typer.BadParameter("--expected-revision must be >= 1")
    _path, store = _control_store(store_dir)
    payload = {
        "approval_id": approval_id,
        "decision": normalized,
        "decided_by": decided_by,
        "expected_revision": expected_revision,
    }
    key = _control_idempotency_key("decide-approval", idempotency_key, payload)
    with _friendly_control_plane_errors():
        record, attempt = store.resolve_approval_for_attempt(
            approval_id,
            approve=normalized == "approve",
            decided_by=decided_by,
            expected_revision=expected_revision,
            idempotency_key=key,
        )
        _emit_control_payload(
            {
                "approval": _sanitized_control_approval(record),
                "attempt": _sanitized_control_attempt(attempt),
                "next_step": (
                    "Launch approval was consumed; the attempt is ready."
                    if normalized == "approve"
                    else "Launch was rejected; the attempt remains on policy hold."
                ),
                "idempotency_key": key,
            },
            json_output=json_output,
        )


def _require_owned_attempt_surface(projection: ControlProjection, attempt_id: str) -> Any:
    attempt = projection.attempts.get(attempt_id)
    if attempt is None:
        raise ControlPlaneError("attempt does not exist")
    agent = projection.agents.get(attempt.agent_id)
    if agent is None:
        raise ControlPlaneError("attempt agent does not exist")
    if agent.adapter != "local_argv" or agent.execution_backend != "subprocess":
        raise ControlPlaneError(
            "external or observed-only agent attempts cannot be controlled; "
            "only agentacct-owned local_argv/subprocess attempts are eligible"
        )
    return attempt


@control_app.command("launch")
def control_launch(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    attempt_id: Annotated[str, typer.Option(help="Pending agentacct-owned attempt id.")],
    expected_revision: Annotated[int, typer.Option(help="Expected current attempt revision.")],
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable retry key; deterministically derived when omitted."),
    ] = None,
    timeout_seconds: Annotated[
        Optional[float],
        typer.Option(
            help=(
                "Optional maximum foreground supervision time. On expiry agentacct safely cancels the exact owned "
                "process; detached one-shot launch is not supported."
            )
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Launch and supervise one owned process in the foreground until terminal."""

    if expected_revision < 1:
        raise typer.BadParameter("--expected-revision must be >= 1")
    if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise typer.BadParameter("--timeout-seconds must be finite and > 0")
    path, store = _control_store(store_dir)
    payload = {
        "attempt_id": attempt_id,
        "expected_revision": expected_revision,
        "timeout_seconds": timeout_seconds,
    }
    key = _control_idempotency_key("launch-attempt", idempotency_key, payload)
    with _friendly_control_plane_errors():
        projection = store.project()
        _require_owned_attempt_surface(projection, attempt_id)
        _require_control_revision(
            projection,
            collection="attempts",
            identifier=attempt_id,
            expected_revision=expected_revision,
            idempotency_key=key,
            idempotent_action="attempt_transitioned",
            idempotent_next_state="launching",
        )
        supervisor = OwnedSupervisor(path, control_store=store)
        timed_out = False
        try:
            result = supervisor.launch_attempt(attempt_id, idempotency_key=key)
            deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
            while True:
                try:
                    wait_window = (
                        1.0
                        if deadline is None
                        else max(0.01, min(1.0, deadline - time.monotonic()))
                    )
                    attempt = supervisor.wait(attempt_id, timeout=wait_window)
                    break
                except TimeoutError:
                    if not supervisor.lease.heartbeat():
                        raise SupervisorError("foreground supervisor lease was lost")
                    if deadline is not None and time.monotonic() >= deadline:
                        attempt = supervisor.cancel_attempt(
                            attempt_id,
                            idempotency_key=_control_child_key(key, "timeout-cancel"),
                        )
                        timed_out = True
                        break
                except KeyboardInterrupt:
                    supervisor.cancel_attempt(
                        attempt_id,
                        idempotency_key=_control_child_key(key, "interrupt-cancel"),
                    )
                    print("Foreground launch interrupted; the exact agentacct-owned process was cancelled.", file=sys.stderr)
                    raise typer.Exit(130)
        finally:
            supervisor.close()
        _emit_control_payload(
            {
                "attempt": _sanitized_control_attempt(attempt),
                "launch_committed_revision": result.attempt.revision,
                "foreground_supervised": True,
                "timed_out": timed_out,
                "idempotency_key": key,
            },
            json_output=json_output,
        )


@control_app.command("cancel")
def control_cancel(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    attempt_id: Annotated[str, typer.Option(help="Running agentacct-owned attempt id.")],
    expected_revision: Annotated[int, typer.Option(help="Expected current attempt revision.")],
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable retry key; deterministically derived when omitted."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Cancel only a live process whose complete agentacct ownership proof matches."""

    if expected_revision < 1:
        raise typer.BadParameter("--expected-revision must be >= 1")
    path, store = _control_store(store_dir)
    payload = {"attempt_id": attempt_id, "expected_revision": expected_revision}
    key = _control_idempotency_key("cancel-attempt", idempotency_key, payload)
    with _friendly_control_plane_errors():
        projection = store.project()
        _require_owned_attempt_surface(projection, attempt_id)
        _require_control_revision(
            projection,
            collection="attempts",
            identifier=attempt_id,
            expected_revision=expected_revision,
            idempotency_key=key,
            idempotent_action="attempt_transitioned",
            idempotent_next_state="cancel_requested",
        )
        supervisor = OwnedSupervisor(path, control_store=store)
        try:
            attempt = supervisor.cancel_attempt(attempt_id, idempotency_key=key)
        finally:
            supervisor.close()
        _emit_control_payload(
            {"attempt": _sanitized_control_attempt(attempt), "idempotency_key": key},
            json_output=json_output,
        )


@control_app.command("retry")
def control_retry(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    attempt_id: Annotated[str, typer.Option(help="Failed, cancelled, or lost source attempt id.")],
    expected_revision: Annotated[int, typer.Option(help="Expected source attempt revision.")],
    new_attempt_id: Annotated[
        Optional[str],
        typer.Option(help="Explicit id for the new pending attempt; derived when omitted."),
    ] = None,
    idempotency_key: Annotated[
        Optional[str],
        typer.Option(help="Stable retry key; deterministically derived when omitted."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Create a new pending attempt from a terminal owned attempt; never relaunch in place."""

    if expected_revision < 1:
        raise typer.BadParameter("--expected-revision must be >= 1")
    _path, store = _control_store(store_dir)
    payload = {
        "source_attempt_id": attempt_id,
        "expected_revision": expected_revision,
        "new_attempt_id": new_attempt_id,
    }
    key = _control_idempotency_key("retry-attempt", idempotency_key, payload)
    with _friendly_control_plane_errors():
        projection = store.project()
        source = _require_owned_attempt_surface(projection, attempt_id)
        previous = projection.idempotency.get(key)
        idempotent_retry = previous is not None and previous.action == "attempt_created"
        if previous is not None and not idempotent_retry:
            raise ControlPlaneError("idempotency key belongs to a different control operation")
        if source.revision != expected_revision and not idempotent_retry:
            raise RevisionConflict(
                f"{attempt_id} revision is {source.revision}, expected {expected_revision}"
            )
        if source.execution_state not in {"failed", "cancelled", "lost"}:
            raise ControlPlaneError(
                "retry requires a failed, cancelled, or lost source attempt; "
                "running and successful attempts are not retried"
            )
        agent = projection.agents[source.agent_id]
        if not agent.enabled:
            raise ControlPlaneError("retry source agent is disabled")
        if idempotent_retry:
            assert previous is not None
            recorded = previous.payload.get("record")
            if not isinstance(recorded, dict):
                raise ControlPlaneError("recorded retry attempt is invalid")
            if recorded.get("task_id") != source.task_id or recorded.get("agent_id") != source.agent_id:
                raise ControlPlaneError("idempotency key belongs to a different retry source")
            recorded_attempt_id = str(recorded.get("attempt_id") or "")
            if new_attempt_id is not None and new_attempt_id != recorded_attempt_id:
                raise ControlPlaneError("idempotency key was already used with a different new attempt id")
            effective_attempt_id = recorded_attempt_id
            effective_workspace_id = str(recorded.get("workspace_id") or "")
            effective_contract_revision = int(recorded.get("contract_revision") or 0)
            effective_initial_control_state = str(recorded.get("control_state") or "")
        else:
            contract = projection.contracts.get(source.task_id)
            if contract is None:
                raise ControlPlaneError("retry source task has no current Task Contract")
            workspace = projection.workspaces.get(contract.workspace_id)
            if workspace is None or not workspace.enabled:
                raise ControlPlaneError("retry requires the current Task Contract workspace to be enabled")
            effective_attempt_id = new_attempt_id
            effective_workspace_id = contract.workspace_id
            effective_contract_revision = contract.revision
            effective_initial_control_state = (
                "awaiting_approval"
                if contract_requires_launch_approval(contract.permission_envelope)
                else "ready"
            )
        attempt = store.create_attempt(
            source.task_id,
            agent_id=source.agent_id,
            workspace_id=effective_workspace_id,
            contract_revision=effective_contract_revision,
            attempt_id=effective_attempt_id,
            initial_control_state=effective_initial_control_state,
            idempotency_key=key,
        )
        _emit_control_payload(
            {
                "source_attempt_id": source.attempt_id,
                "attempt": _sanitized_control_attempt(attempt),
                "launch_approval_required": attempt.control_state == "awaiting_approval",
                "next_step": (
                    "Request launch approval for this held retry attempt."
                    if attempt.control_state == "awaiting_approval"
                    else "Launch this ready retry attempt when you want execution to begin."
                ),
                "idempotency_key": key,
            },
            json_output=json_output,
        )


@control_app.command("reconcile")
def control_reconcile(
    store_dir: Annotated[Path, typer.Option(help="Explicit agentacct state directory.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit compact machine-readable JSON.")] = False,
) -> None:
    """Classify owned proofs now; ambiguous exits become lost, with no detached monitor claim."""

    path, store = _control_store(store_dir)
    with _friendly_control_plane_errors():
        supervisor = OwnedSupervisor(path, control_store=store)
        try:
            result = supervisor.reconcile()
        finally:
            supervisor.close()
        _emit_control_payload(
            {
                "schema_version": "agent-chronicle.control-reconcile.v1",
                "classification": {
                    "verified_live_owned": result["live"],
                    "marked_lost": result["lost"],
                    "legacy_marked_lost": result["legacy_lost"],
                },
                "persistent_monitoring": False,
                "next_step_for_live": (
                    "Keep the dashboard control runtime running for persistent supervision."
                    if result["live"]
                    else None
                ),
                "mutation_idempotency": "supervisor-internal",
            },
            json_output=json_output,
        )


@control_app.command("evaluate")
def control_evaluate(
    action: Annotated[str, typer.Option(help="Recommended action: warn, pause, cancel, or block.")],
    target_id: Annotated[str, typer.Option(help="Controller-owned target identifier.")],
    recommendation: Annotated[str, typer.Option(help="Human-readable recommendation.")],
    requested_mode: Annotated[str, typer.Option(help="advisory (default) or hard eligibility evaluation.")] = "advisory",
    evidence_basis: Annotated[str, typer.Option(help="Evidence basis, e.g. provider_billed or estimated_from_tokens.")] = "unknown",
    cost_confidence: Annotated[str, typer.Option(help="Cost confidence label.")] = "unknown",
    target_type: Annotated[str, typer.Option(help="Target type, usually execution.")] = "execution",
    evidence_id: Annotated[Optional[list[str]], typer.Option("--evidence-id", help="Supporting Evidence v2 id; repeatable.")] = None,
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    explicit_conservative_approval: Annotated[bool, typer.Option(help="User explicitly approved a conservative non-billed basis.")] = False,
    controller_owns_execution: Annotated[bool, typer.Option(help="The downstream controller proves it owns this execution.")] = False,
    conflicting: Annotated[bool, typer.Option(help="Supporting evidence is known to conflict.")] = False,
    expires_at: Annotated[Optional[str], typer.Option(help="Optional ISO-8601 signal expiry.")] = None,
    idempotency_key: Annotated[Optional[str], typer.Option(help="Required for hard-action eligibility.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Evaluate a signal only; agentacct never dispatches an external action."""

    if action not in {"warn", "pause", "cancel", "block"}:
        raise typer.BadParameter("--action must be warn, pause, cancel, or block")
    if requested_mode not in {"advisory", "hard"}:
        raise typer.BadParameter("--requested-mode must be advisory or hard")
    try:
        signal = ControlSignal(
            action=action,  # type: ignore[arg-type]
            target_type=target_type,
            target_id=target_id,
            recommendation=recommendation,
            requested_mode=requested_mode,  # type: ignore[arg-type]
            evidence_basis=evidence_basis,
            cost_confidence=cost_confidence,
            supporting_evidence_ids=tuple(evidence_id or ()),
            explicit_conservative_approval=explicit_conservative_approval,
            controller_owns_execution=controller_owns_execution,
            conflicting=conflicting,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    runtime = None
    if requested_mode == "hard" or signal.supporting_evidence_ids or store_dir is not None:
        runtime = EvidenceRuntime(_resolve_cli_store_dir(store_dir).path)
    validation = validate_supporting_evidence(signal, runtime)
    decision = evaluate_control_signal(signal, supporting_evidence=validation)
    payload = {
        "signal_id": decision.signal_id,
        "requested_mode": decision.requested_mode,
        "effective_mode": decision.effective_mode,
        "hard_enforcement_allowed": decision.hard_enforcement_allowed,
        "reason": decision.reason,
        "supporting_evidence_validation": validation.to_dict(),
        "external_action_dispatched": False,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Signal: {payload['signal_id']}")
    print(f"Effective mode: {payload['effective_mode']}")
    print(f"Hard enforcement allowed: {str(payload['hard_enforcement_allowed']).lower()}")
    print(f"Reason: {payload['reason']}")
    print(f"Supporting evidence: {payload['supporting_evidence_validation']['state']}")
    print("External action dispatched: false")


def _validate_usage_import_args(client: str, limit_sessions: int) -> None:
    allowed = {"all", *SUPPORTED_CLIENTS}
    if client not in allowed:
        raise typer.BadParameter("--client must be one of: " + ", ".join(sorted(allowed)))
    if limit_sessions < 1 or limit_sessions > 200:
        raise typer.BadParameter("--limit-sessions must be between 1 and 200")


def _usage_importer_version() -> str:
    return importer_build_id()


def _selected_usage_sources(client: str) -> tuple[str, ...]:
    return tuple(SUPPORTED_CLIENTS) if client == "all" else (client,)


def _local_usage_import_payload(
    *,
    store_dir: Path,
    client: str,
    codex_home: Path | None = None,
    claude_home: Path | None = None,
    opencode_home: Path | None = None,
    hermes_home: Path | None = None,
    openclaw_home: Path | None = None,
    cursor_home: Path | None = None,
    limit_sessions: int = 20,
    dry_run: bool = False,
    estimate_costs: bool = False,
    refresh: bool = False,
) -> dict[str, object]:
    _validate_usage_import_args(client, limit_sessions)
    # Callers resolve the store first (shared resolver, decision 4a).
    effective_store_dir = store_dir
    health_store = None if dry_run else IngestionHealthStore(effective_store_dir)
    scan_id: str | None = None
    previous_catalog_path = os.environ.get(PRICING_CATALOG_PATH_ENV)
    pricing_auto_refresh: dict[str, object] | None = None
    pricing_enabled = estimate_costs and client != "cursor"
    if pricing_enabled:
        # TTL auto-refresh of the store-local LiteLLM snapshot (owner's
        # standing instruction: import prices from LiteLLM's open table, don't
        # hand-maintain). Runs BEFORE activation so a user env pin is seen
        # as-is; best-effort by contract — a failed fetch records itself in
        # the snapshot sidecar and this import proceeds on the stale/builtin
        # catalog. Covers import-local AND every watch tick (cheap TTL stat).
        pricing_auto_refresh = ensure_fresh_pricing_snapshot(effective_store_dir)
        activate_pricing_catalog_for_store(effective_store_dir)
    try:
        if health_store is not None:
            scan_id = health_store.begin_scan(
                sources=_selected_usage_sources(client),
                scan_limit=limit_sessions,
                importer_version=_usage_importer_version(),
            )
        service = SentinelService(effective_store_dir)
        expected_observation_conflicts = (
            service.trusted_session_observation_conflict_snapshot()
        )
        with codex_parse_cache_scope(effective_store_dir):
            discovery = discover_client_usage_with_diagnostics(
                client=client,
                codex_home=codex_home,
                claude_home=claude_home,
                opencode_home=opencode_home,
                hermes_home=hermes_home,
                openclaw_home=openclaw_home,
                cursor_home=cursor_home,
                limit_sessions=limit_sessions,
            )
        scanned_candidates = discovery.events
        session_observation_candidates = usage_less_session_observations(discovery)
        complete_observation_clients = (
            complete_session_observation_reconciliation_clients(discovery)
        )
        resolved_session_observation_conflicts = (
            []
            if dry_run or not complete_observation_clients
            else service.reconcile_trusted_session_observation_conflicts(
                [
                    observation.to_sentinel_event()
                    for observation in discovery.session_observations
                    if observation.client in complete_observation_clients
                ],
                expected_conflict_revisions=expected_observation_conflicts,
            )
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
        recorded_session_observations: list[dict[str, object]] = []
        session_observation_conflicts_by_client: dict[str, int] = {}
        session_observation_conflict_reasons_by_client: dict[
            str, dict[str, int]
        ] = {}
        if not dry_run and session_observation_candidates:
            existing_event_ids = {
                str(event.get("event_id"))
                for event in service.list_all_events()
                if event.get("event_id")
            }
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
        # Provider rate-limit snapshots (CLI phase B foundation): passively read
        # the provider-reported usage-limit windows Codex writes to its session
        # rollout files, the Claude desktop app writes to its plan-usage history,
        # and the terminal-CLI statusLine hook writes to its spool — and record
        # them as rate_limit_observed events. Dedup is TRANSITION-based
        # (record_snapshots_transitionally against each stream's last recorded
        # snapshot; snapshots carry no idempotency_key), so an unchanged limit is
        # a no-op but a change/decline/reset is recorded with a fresh time. The
        # whole block is best-effort and can never fail the usage import.
        rate_limit_snapshots_recorded = 0
        if not dry_run:
            from . import rate_limits as _rate_limits

            _global_scan = _rate_limits.global_limit_scan_enabled()
            _rl_snapshots: list[Any] = []
            try:
                if client in ("all", "codex"):
                    # Codex honors codex_home directly: an explicit home is a
                    # hermetic local scan (always allowed); only the default read
                    # of the real ~/.codex is gated behind the global-scan knob.
                    if codex_home is not None or _global_scan:
                        _codex_rl = _rate_limits.read_codex_rate_limits_latest(codex_home)
                        if _codex_rl is not None:
                            _rl_snapshots.append(_codex_rl)
                # The Claude desktop plan-usage file is a fixed machine-global path
                # (not under claude_home / CLAUDE_CONFIG_DIR). Read it only on a
                # genuinely default scan: no Claude-home relocation (the param OR
                # the CLAUDE_CONFIG_DIR env the import itself honors) and the
                # global-scan knob on. This keeps hermetic/custom scans from ever
                # reaching into the developer's real machine file.
                if (
                    client in ("all", "claude-code")
                    and claude_home is None
                    and not os.environ.get("CLAUDE_CONFIG_DIR")
                    and _global_scan
                ):
                    _rl_snapshots.extend(_rate_limits.read_claude_plan_usage_latest())
                # Terminal-CLI users have no desktop plan-usage file; their limits
                # arrive via the statusLine spool. Its path follows the Claude
                # config home (CLAUDE_CONFIG_DIR / ~/.claude), so it stays isolated
                # under a relocated home — gate only on claude_home + global scan.
                if (
                    client in ("all", "claude-code")
                    and claude_home is None
                    and _global_scan
                ):
                    _statusline_rl = _rate_limits.read_claude_statusline_latest()
                    if _statusline_rl is not None:
                        _rl_snapshots.append(_statusline_rl)
            except Exception:  # noqa: BLE001 - a limits read must never break the usage import.
                _rl_snapshots = []
            if _rl_snapshots:
                try:
                    # Transition-based dedup: record a snapshot only when its
                    # stream's state changed since the last recorded one, so an
                    # unchanged limit re-seen every tick is a no-op but a decline
                    # or reset is recorded with a fresh observation time.
                    rate_limit_snapshots_recorded = (
                        _rate_limits.record_snapshots_transitionally(
                            _rl_snapshots,
                            existing_events=service.list_all_events(),
                            record=service.record_event,
                        )
                    )
                except Exception:  # noqa: BLE001 - best-effort; recording must not fail import.
                    rate_limit_snapshots_recorded = 0
        if not dry_run:
            # Drain the Claude Code hook's tool-category spool into additive
            # tool_activity_observed events (Receipt Actions dimension). Same
            # record=service.record_event contract as the rate-limit spool; the
            # batches are additive and content-idded, so re-import never double
            # counts. Best-effort: a spool error can never fail the usage import.
            from .tool_activity import ingest_tool_activity_spool

            try:
                ingest_tool_activity_spool(
                    effective_store_dir,
                    record=service.record_event,
                    now=time.time(),
                )
            except Exception:  # noqa: BLE001 - draining the spool must never fail the import.
                pass
            # Drain the PostToolUse mechanical-check spool into client_hook
            # Evidence-v2 envelopes — the independent (harness-observed) checks
            # that lift a step to independently_checked. Best-effort, same as
            # above: a spool/evidence error can never fail the usage import.
            from .mechanical_capture import ingest_mechanical_check_spool

            try:
                ingest_mechanical_check_spool(
                    effective_store_dir,
                    evidence=service.evidence,
                    now=time.time(),
                )
            except Exception:  # noqa: BLE001 - draining the spool must never fail the import.
                pass
        if dry_run:
            projectable_observation_identities: set[tuple[str, str, str]] = set()
            observation_conflict_identities = 0
        else:
            current_events = service.list_all_events()
            projectable_observation_identities = (
                selected_local_session_observation_source_identities(
                    current_events
                )
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
        ) = (
            bind_discovered_usage_source_namespaces(
                service,
                scanned_candidates,
                write=not dry_run,
            )
        )
        plan = plan_local_usage_import(candidates, service.list_all_events())
        # issue #53: sessions whose usage DID parse but were withheld from import
        # as incomplete (a selected transcript could not be fully read this scan).
        # Captured here — before any later plan reassignment — so the CLI can fail
        # loudly instead of reporting a silent $0 for a measurement gap.
        withheld_incomplete_session_count = len(
            {
                (candidate.client, candidate.client_session_id)
                for candidate in plan.incomplete_source_candidates
            }
        )
        refresh_candidates_before_reprice = list(plan.refresh_candidates)
        refreshed_candidate_count = len(plan.refresh_candidates)
        # Unknown→priced reprice (refresh + estimate-costs only): stored rows
        # whose cost is unknown but whose (provider, model) now resolves in
        # the catalog are promoted to refresh-worthy and replaced with priced
        # rows. Already-priced rows keep the Phase-3 stability rule.
        repriced_candidates: list = []
        if refresh and estimate_costs:
            plan, repriced_candidates = promote_unknown_cost_reprices(plan)
        # Never-refresh invariant (the default): refresh_candidates (identity
        # already stored) are skipped; only genuinely-new rows and one-shot
        # legacy ':model:' key migrations write anything. --refresh opts in to
        # the dashboard's replace semantics instead: re-observed identities are
        # superseded with fresh totals in the same transaction.
        import_candidates = select_usage_import_candidates(plan, include_refresh=refresh)
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
        if estimate_costs:
            for event in events:
                if apply_pricing_estimate_to_event(event):
                    identity = recognized_local_usage_row_identity(event)
                    if identity is not None:
                        planned_priced_identities.add(identity)
        if dry_run or not events:
            # Nothing to write: never rewrite the ledger file for a no-op scan.
            recorded = []
        else:
            # The common path is one bounded full-ledger rewrite. Rare legacy
            # migration/adoption cohorts use separate per-base guarded writes
            # so their lanes remain atomic without making a 500-session scan
            # rewrite the ledger 500 times. Each replace_events call re-reads
            # under the write lock; its batch-specific dedup key makes new
            # bases conditional on staying empty and ordinary refreshes
            # conditional on their exact row identity/revision.
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
        if dry_run:
            write_namespace_conflicts: list = []
            concurrent_refresh_conflicts: list = []
        else:
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
        actual_refresh_count = sum(
            1
            for candidate in refresh_candidates_before_reprice
            if candidate.usage_row_identity in recorded_identities
        )
        actual_repriced_count = sum(
            1
            for candidate in repriced_candidates
            if candidate.usage_row_identity in recorded_identities
        )
        actual_migration_candidates = [
            candidate
            for candidate in plan.migration_candidates
            if candidate.usage_row_identity in recorded_identities
        ]
        actual_migration_bases = {
            (
                candidate.client,
                normalized_local_usage_session_id(
                    candidate.client,
                    candidate.client_session_id,
                ),
            )
            for candidate in actual_migration_candidates
        }
        actual_superseded_legacy_rows = sum(
            len(plan.replaced_alias_keys_by_base.get(base, []))
            for base in actual_migration_bases
        )
        effective_events = events if dry_run else recorded
        additive_effective_events = [
            event for event in effective_events if local_usage_event_additivity(event)[0]
        ]
        excluded_non_additive_events = len(effective_events) - len(additive_effective_events)
        usage_exclusion_reasons = {
            reason
            for event in effective_events
            for additive, reason in [local_usage_event_additivity(event)]
            if not additive and reason
        }
        usage_exclusion_reason = (
            next(iter(usage_exclusion_reasons))
            if len(usage_exclusion_reasons) == 1
            else "mixed_source_identity_or_lineage_normalization"
            if usage_exclusion_reasons
            else CODEX_REPLAY_QUARANTINE_STATE
        )
        priced_events = (
            len(planned_priced_identities)
            if dry_run
            else sum(
                1
                for event in recorded
                if recognized_local_usage_row_identity(event) in planned_priced_identities
            )
        )
        write_namespace_adoptions = (
            planned_write_namespace_adoptions
            if dry_run
            else [
                candidate
                for candidate in planned_write_namespace_adoptions
                if candidate.usage_row_identity in recorded_identities
            ]
        )
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
        cost_confidence_counts: dict[str, int] = {}
        for event in additive_effective_events:
            confidence = str(event.get("cost_confidence") or "unknown")
            cost_confidence_counts[confidence] = cost_confidence_counts.get(confidence, 0) + 1
        if not cost_confidence_counts:
            summary_cost_confidence = "unknown"
        elif len(cost_confidence_counts) == 1:
            summary_cost_confidence = next(iter(cost_confidence_counts))
        else:
            summary_cost_confidence = "mixed"
        evidence_usage_reconcile: dict[str, object]
        if dry_run:
            evidence_usage_reconcile = {
                "enabled": service.evidence.enabled,
                "skipped": True,
                "reason": "dry_run",
            }
        else:
            # Reconcile the complete current v1 ledger on every successful
            # persisted scan, even when this tick rewrote zero rows. That is
            # the fail-open compensation path: if a prior Evidence projection
            # failed after v1 committed, the next unchanged tick self-heals.
            # The service holds the v1 writer lock while a complete snapshot is
            # applied; corrupt ledger lines automatically withhold deletion.
            try:
                evidence_usage_reconcile = (
                    service.reconcile_evidence_refreshable_usage_snapshot(
                        complete=True,
                        transport="internal",
                    )
                )
            except Exception:  # noqa: BLE001 - Evidence must not roll back a proven v1 write.
                evidence_usage_reconcile = {
                    "enabled": bool(
                        getattr(getattr(service, "evidence", None), "enabled", True)
                    ),
                    "complete_requested": True,
                    "complete_applied": False,
                    "errors": [EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE],
                }
            if not isinstance(evidence_usage_reconcile, Mapping):
                evidence_usage_reconcile = {
                    "enabled": bool(
                        getattr(getattr(service, "evidence", None), "enabled", True)
                    ),
                    "complete_requested": True,
                    "complete_applied": False,
                    "errors": [EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE],
                }
            evidence_usage_reconcile = dict(evidence_usage_reconcile)
        payload: dict[str, object] = {
            "client": client,
            "dry_run": dry_run,
            "refresh": refresh,
            # Exactly the client homes this scan inspected (client filter +
            # --*-home overrides applied), so a zero-result hint never claims
            # scans that did not happen.
            "scanned_homes": describe_scanned_client_homes(
                client=client,
                codex_home=codex_home,
                claude_home=claude_home,
                opencode_home=opencode_home,
                hermes_home=hermes_home,
                openclaw_home=openclaw_home,
                cursor_home=cursor_home,
            ),
            "scanned_sessions": len(observed_session_keys),
            "observed_sessions": len(observed_session_keys),
            "usage_sessions": len(usage_session_keys),
            "sessions_without_usage": len(session_observation_candidates),
            "eligible_session_observations": len(session_observation_candidates),
            "imported_session_observations": len(recorded_session_observations),
            "rate_limit_snapshots_recorded": rate_limit_snapshots_recorded,
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
            "session_observation_conflict_reasons": {
                code: sum(
                    counts.get(code, 0)
                    for counts in session_observation_conflict_reasons_by_client.values()
                )
                for code in sorted(
                    {
                        reason
                        for counts in session_observation_conflict_reasons_by_client.values()
                        for reason in counts
                    }
                )
            },
            "session_observation_conflict_reasons_by_client": {
                source: dict(sorted(counts.items()))
                for source, counts in sorted(
                    session_observation_conflict_reasons_by_client.items()
                )
            },
            "source_diagnostics": import_diagnostics,
            "importable_sessions": len(import_candidates),
            "withheld_incomplete_sessions": withheld_incomplete_session_count,
            "imported_events": len(recorded),
            "refreshed_events": (
                refreshed_candidate_count if dry_run and refresh else actual_refresh_count if refresh else 0
            ),
            "repriced_events": len(repriced_candidates) if dry_run else actual_repriced_count,
            "pricing_auto_refresh": pricing_auto_refresh,
            "evidence_refreshable_usage": evidence_usage_reconcile,
            # Client-log evidenced links over ALL scanned sessions (install-
            # time verification: dry-run shows what the client logs prove,
            # regardless of which rows this command writes).
            "client_log_evidence": {
                "sessions_with_evidenced_events": sum(
                    1
                    for candidate in [*scanned_candidates, *session_observation_candidates]
                    if candidate.evidenced_event_id_total
                ),
                "evidenced_event_ids_total": sum(
                    candidate.evidenced_event_id_total
                    for candidate in [*scanned_candidates, *session_observation_candidates]
                ),
                "evidenced_outputs_skipped": sum(
                    candidate.evidenced_outputs_skipped
                    for candidate in [*scanned_candidates, *session_observation_candidates]
                ),
            },
            "migrated_events": (
                len(plan.migration_candidates) if dry_run else len(actual_migration_candidates)
            ),
            "source_namespace_conflicts": sum(
                int(row.get("source_namespace_conflicts") or 0)
                for row in import_diagnostics.values()
            ),
            "source_namespace_adoptions": sum(namespace_adoption_counts.values()),
            "concurrent_refresh_conflicts": len(concurrent_refresh_conflicts),
            "incomplete_alias_migrations": len(plan.incomplete_migration_bases),
            "superseded_legacy_rows": (
                sum(len(keys) for keys in plan.replaced_alias_keys_by_base.values())
                if dry_run
                else actual_superseded_legacy_rows
            ),
            "priced_events": priced_events,
            "pricing_catalog_path": read_env_alias(PRICING_CATALOG_PATH_ENV),
            "cost_confidence": summary_cost_confidence,
            "cost_confidence_counts": cost_confidence_counts,
            "usage_exclusions": {
                "non_additive_rows": excluded_non_additive_events,
                "reason": usage_exclusion_reason,
                "raw_evidence_preserved": True,
            },
            "usage_totals": {
                "input_tokens": sum(event["estimated_input_tokens"] or 0 for event in additive_effective_events),
                "output_tokens": sum(event["estimated_output_tokens"] or 0 for event in additive_effective_events),
                "cached_input_tokens": sum(int(event["metadata"].get("cached_input_tokens") or 0) for event in additive_effective_events),
                "cache_creation_input_tokens": sum(int(event["metadata"].get("cache_creation_input_tokens") or 0) for event in additive_effective_events),
                "cache_read_input_tokens": sum(int(event["metadata"].get("cache_read_input_tokens") or 0) for event in additive_effective_events),
                "estimated_cost_usd": sum(float(event.get("estimated_cost_usd") or 0.0) for event in additive_effective_events),
            },
            "events": recorded if not dry_run else events,
            "session_observation_events": (
                recorded_session_observations if not dry_run else []
            ),
        }
        if health_store is not None and scan_id is not None:
            health_results = apply_evidence_refreshable_usage_health(
                health_scan_results(import_diagnostics),
                sources=_selected_usage_sources(client),
                outcome=evidence_usage_reconcile,
            )
            health_store.complete_scan(
                scan_id,
                results=health_results,
            )
            scan_id = None
            payload["ingestion_health"] = health_store.snapshot()
        return payload
    except Exception:
        if health_store is not None and scan_id is not None:
            health_store.fail_scan(scan_id, error_code="import_failed")
        raise
    finally:
        if pricing_enabled:
            _restore_pricing_catalog_env(previous_catalog_path)


def _print_usage_import_payload(payload: dict[str, object], *, store_dir: Path, estimate_costs: bool) -> None:
    usage_totals = payload.get("usage_totals") if isinstance(payload.get("usage_totals"), dict) else {}
    is_dry_run = bool(payload.get("dry_run"))
    observation_only = payload.get("client") == "cursor"
    action = "Would import" if is_dry_run else "Imported"
    imported_count = (
        payload.get("importable_sessions", 0)
        if is_dry_run
        else payload.get("imported_events", 0)
    )
    if observation_only:
        observation_count = (
            payload.get("eligible_session_observations", 0)
            if is_dry_run
            else payload.get("imported_session_observations", 0)
        )
        verb = "Would preserve" if is_dry_run else "Preserved"
        print(
            f"{verb} {observation_count} Cursor session observation(s); "
            "usage, cache, and cost are unavailable by design."
        )
    else:
        print(f"{action} {imported_count} local client usage session(s).")
    if payload.get("sessions_without_usage"):
        if is_dry_run:
            print(
                f"Found {payload.get('eligible_session_observations', 0)} observed session(s) eligible "
                "for usage-unavailable preservation; conflicts are evaluated only on write."
            )
        elif payload.get("preserved_session_observations"):
            print(
                f"Preserved {payload.get('preserved_session_observations', 0)} observed session(s) "
                "with usage unavailable (no zero token or zero cost was invented)."
            )
    if payload.get("refresh"):
        refresh_action = "Would replace" if payload.get("dry_run") else "Replaced"
        print(
            f"{refresh_action} {payload.get('refreshed_events', 0)} re-observed session row(s) "
            "with fresh totals (--refresh)."
        )
    if payload.get("repriced_events"):
        reprice_action = "Would replace" if payload.get("dry_run") else "Replaced"
        print(
            f"{reprice_action} {payload.get('repriced_events', 0)} previously unknown-cost row(s) "
            "whose model now resolves in the pricing catalog (unknown→priced only)."
        )
    if not payload.get("scanned_sessions"):
        scanned_homes = payload.get("scanned_homes") if isinstance(payload.get("scanned_homes"), list) else []
        checked = "; ".join(str(home) for home in scanned_homes) if scanned_homes else "no client homes"
        print(
            f"No local client session files found (checked {checked}). "
            "Run `agentacct usage discover-sources` to see what was scanned, "
            "or point at a custom location with --claude-home/--codex-home/--cursor-home "
            "or the matching client --*-home option."
        )
    if payload.get("migrated_events"):
        migration_action = "Would supersede" if payload.get("dry_run") else "Superseded"
        print(
            f"{migration_action} {payload.get('superseded_legacy_rows', 0)} legacy per-model-keyed row(s) "
            f"with {payload.get('migrated_events', 0)} stable-key row(s); "
            "superseded keys are kept in each row's provenance metadata."
        )
    if payload.get("source_namespace_conflicts"):
        namespace_action = "Would skip" if payload.get("dry_run") else "Skipped"
        print(
            f"{namespace_action} {payload.get('source_namespace_conflicts', 0)} session row(s) "
            "because the same client session id was previously imported from a different client home. "
            "Re-run with the intended --codex-home/--claude-home; "
            "agentacct will not overwrite that identity across source namespaces."
        )
    if payload.get("source_namespace_adoptions"):
        adoption_action = "Would adopt" if payload.get("dry_run") else "Adopted"
        print(
            f"{adoption_action} source-home provenance for {payload.get('source_namespace_adoptions', 0)} "
            "legacy usage row(s) from this explicit source scan."
        )
    if payload.get("concurrent_refresh_conflicts"):
        print(
            f"Skipped {payload.get('concurrent_refresh_conflicts', 0)} stale refresh row(s) because another "
            "refresh saved a newer revision first. Re-run refresh to inspect the latest totals."
        )
    if payload.get("incomplete_alias_migrations"):
        print(
            f"Preserved {payload.get('incomplete_alias_migrations', 0)} legacy session(s) because "
            "the current source scan did not reproduce every stored model lane. Repair the client log "
            "or source path, then refresh again."
        )
    usage_exclusions = payload.get("usage_exclusions") if isinstance(payload.get("usage_exclusions"), dict) else {}
    if int(usage_exclusions.get("non_additive_rows") or 0):
        print(
            f"Held {usage_exclusions.get('non_additive_rows', 0)} usage row(s) "
            "out of token and cost totals until source identity or lineage normalization; raw rows remain saved."
        )
    if not observation_only:
        print(
            "Tokens: "
            f"input={usage_totals.get('input_tokens', 0)} "
            f"output={usage_totals.get('output_tokens', 0)} "
            f"cache_create={usage_totals.get('cache_creation_input_tokens', 0)} "
            f"cache_read={usage_totals.get('cache_read_input_tokens', 0)} "
            f"cached_input={usage_totals.get('cached_input_tokens', 0)}"
        )
        evidence = payload.get("client_log_evidence") if isinstance(payload.get("client_log_evidence"), dict) else {}
        print(
            "Client-log evidenced links: "
            f"{evidence.get('sessions_with_evidenced_events', 0)} scanned session(s) carry "
            f"{evidence.get('evidenced_event_ids_total', 0)} recorded-event id(s) paired from the client's own session logs "
            f"({evidence.get('evidenced_outputs_skipped', 0)} unpaired output(s) skipped)."
        )
        print("Usage confidence: client_reported; cost confidence: " + str(payload.get("cost_confidence") or "unknown"))
    if estimate_costs and not observation_only:
        print(
            "Estimated equivalent cost from pricing table: "
            f"${float(usage_totals.get('estimated_cost_usd') or 0.0):.6f} "
            f"({payload.get('priced_events', 0)} priced session(s); not provider billing)"
        )
    _print_evidence_refreshable_usage_warning(payload)
    print(f"Local API: agentacct serve --store-dir {shlex.quote(str(store_dir))}")


def _print_evidence_refreshable_usage_warning(
    payload: Mapping[str, object],
    *,
    prefix: str = "",
) -> None:
    if "evidence_refreshable_usage" not in payload:
        return
    outcome = payload.get("evidence_refreshable_usage")
    if evidence_refreshable_usage_failed(outcome):
        outcome_map = outcome if isinstance(outcome, Mapping) else {}
        errors = outcome_map.get("errors")
        error_count = (
            len(errors)
            if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes))
            else int(bool(errors))
        )
        conflict_count = int(outcome_map.get("conflicts") or 0)
        print(
            f"{prefix}WARNING: the local usage ledger was saved, but Evidence v2 "
            "current-usage reconciliation is not healthy "
            f"(errors={error_count} conflicts={conflict_count}). "
            "Retry usage refresh and inspect "
            "ingestion health before any Evidence rebuild or cleanup.",
            file=sys.stderr,
            flush=True,
        )


@usage_app.command("import-local")
def usage_import_local(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    client: Annotated[str, typer.Option(help="Client to import: all, codex, claude-code, opencode, hermes, openclaw, or observation-only cursor.")] = "all",
    codex_home: Annotated[Optional[Path], typer.Option(help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.")] = None,
    claude_home: Annotated[Optional[Path], typer.Option(help="Claude Code home directory. Defaults to CLAUDE_CONFIG_DIR, then XDG and ~/.claude homes.")] = None,
    opencode_home: Annotated[Optional[Path], typer.Option(help="OpenCode home/export directory. Defaults to ~/.local/share/opencode.")] = None,
    hermes_home: Annotated[Optional[Path], typer.Option(help="Hermes home directory. Defaults to ~/.hermes.")] = None,
    openclaw_home: Annotated[Optional[Path], typer.Option(help="OpenClaw home directory. Defaults to ~/.openclaw and related roots.")] = None,
    cursor_home: Annotated[Optional[Path], typer.Option(help="Cursor application-support root. Defaults to ~/Library/Application Support/Cursor; only User/globalStorage/state.vscdb is inspected.")] = None,
    limit_sessions: Annotated[int, typer.Option(help="Recent sessions to inspect per client.")] = 20,
    dry_run: Annotated[bool, typer.Option(help="Preview importable usage without writing agentacct events.")] = False,
    estimate_costs: Annotated[bool, typer.Option(help="Estimate equivalent cost from local token counts using agentacct's pricing table when the model is known. Not provider billing.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Also update already-imported sessions: replace each re-observed row whose totals CHANGED with fresh totals (like the dashboard's 'Refresh & save usage' button, except the dashboard always recomputes pricing estimates while the CLI only does so with --estimate-costs). Unchanged rows are left untouched. Default: each session is imported once at first observation and never updated.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Import local usage or observation-only session facts from client stores.

    This command scans local client session/export files for usage fields, does not
    read credentials, does not call provider APIs, and stores only summarized
    token counts in agentacct's event ledger. Cursor contributes session/model
    observations only and never token or cost rows. By default each session is
    imported once at first observation and never updated afterward; pass
    --refresh to replace re-observed usage rows with fresh totals.
    """
    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    payload = _local_usage_import_payload(
        store_dir=resolved_store_dir,
        client=client,
        codex_home=codex_home,
        claude_home=claude_home,
        opencode_home=opencode_home,
        hermes_home=hermes_home,
        openclaw_home=openclaw_home,
        cursor_home=cursor_home,
        limit_sessions=limit_sessions,
        dry_run=dry_run,
        estimate_costs=estimate_costs,
        refresh=refresh,
    )
    if json_output:
        # Machine consumers detect a measurement gap via the payload's
        # withheld_incomplete_sessions field; keep stdout clean + exit 0.
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    _print_usage_import_payload(payload, store_dir=resolved_store_dir, estimate_costs=estimate_costs)
    # issue #53: a Claude-side pipeline failure used to report a silent $0 with
    # exit 0. If usage parsed but was withheld as incomplete, say so loudly on
    # stderr and exit non-zero so the gap is never read as "nothing was spent".
    # Human CLI surface only; --json returns above, and the watch daemon, TUI,
    # onboard, and dashboard callers use the payload directly and are unaffected.
    withheld = int(payload.get("withheld_incomplete_sessions", 0) or 0)
    if withheld:
        print(
            f"WARNING: {withheld} session(s) had usage that could not be fully read "
            "this scan and was withheld from this import — a measurement gap, not $0 "
            "spent (any session imported on an earlier scan keeps its prior totals). "
            "Re-run the import; if it persists, please report it at "
            "https://github.com/mikehasa/agentacct/issues.",
            file=sys.stderr,
            flush=True,
        )
        raise typer.Exit(3)


@usage_app.command("watch")
def usage_watch(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    client: Annotated[str, typer.Option(help="Client to import: all, codex, claude-code, opencode, hermes, openclaw, or observation-only cursor.")] = "all",
    codex_home: Annotated[Optional[Path], typer.Option(help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.")] = None,
    claude_home: Annotated[Optional[Path], typer.Option(help="Claude Code home directory. Defaults to CLAUDE_CONFIG_DIR, then XDG and ~/.claude homes.")] = None,
    opencode_home: Annotated[Optional[Path], typer.Option(help="OpenCode home/export directory. Defaults to ~/.local/share/opencode.")] = None,
    hermes_home: Annotated[Optional[Path], typer.Option(help="Hermes home directory. Defaults to ~/.hermes.")] = None,
    openclaw_home: Annotated[Optional[Path], typer.Option(help="OpenClaw home directory. Defaults to ~/.openclaw and related roots.")] = None,
    cursor_home: Annotated[Optional[Path], typer.Option(help="Cursor application-support root. Defaults to ~/Library/Application Support/Cursor; only User/globalStorage/state.vscdb is inspected.")] = None,
    interval_seconds: Annotated[float, typer.Option(help="Seconds between import scans when running continuously.")] = 60.0,
    limit_sessions: Annotated[int, typer.Option(help="Recent sessions to inspect per client per scan.")] = 20,
    estimate_costs: Annotated[bool, typer.Option(help="Estimate equivalent cost from known model pricing rows. Not provider billing.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Also update already-imported sessions on every scan: replace each re-observed row whose totals CHANGED with fresh totals (like the dashboard's 'Refresh & save usage' button, except the dashboard always recomputes pricing estimates while the CLI only does so with --estimate-costs). Unchanged rows are left untouched, so idle sessions never churn the ledger. Default: each session is imported once at first observation and never updated.")] = False,
    once: Annotated[bool, typer.Option(help="Run one scan and exit. Useful for cron, launchd, and smoke tests.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit one JSON payload per scan.")] = False,
) -> None:
    """Continuously import local usage and observation-only session facts.

    By default each session is imported once at first observation and never
    updated afterward; pass --refresh to keep growing sessions' totals current
    by replacing re-observed rows on every scan.
    """

    _validate_usage_import_args(client, limit_sessions)
    if interval_seconds < 1:
        raise typer.BadParameter("--interval-seconds must be >= 1")
    # Resolve once at startup so daemons fail fast with the actionable
    # resolution error instead of failing on some later scan.
    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    health_store = IngestionHealthStore(resolved_store_dir)
    # Canonical read-model maintenance (migration phase 4.3): rebuilds run
    # HERE, in the watcher loop after a successful scan — never in a read
    # request path. The policy is live-write-flag gated, stale-only, and
    # cooldown-bounded; disabled it does nothing at all.
    from .canonical_live import CanonicalRebuildPolicy

    canonical_rebuild_policy = CanonicalRebuildPolicy(resolved_store_dir)
    lease_id: str | None = None
    stop_requested = threading.Event()
    previous_sigterm: Any = None
    if not once and threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_requested.set())
    try:
        if not once:
            candidate_lease_id = f"watch-{uuid.uuid4().hex}"
            acquisition = health_store.acquire_watcher(
                lease_id=candidate_lease_id,
                pid=os.getpid(),
                importer_version=_usage_importer_version(),
                interval_seconds=interval_seconds,
                scan_limit=limit_sessions,
                sources=_selected_usage_sources(client),
            )
            if not acquisition.acquired:
                raise typer.BadParameter(
                    "an active usage watcher already owns this store; stop or restart that watcher before starting another"
                )
            lease_id = candidate_lease_id
        while not stop_requested.is_set():
            if lease_id is not None and not health_store.heartbeat_watcher(lease_id):
                raise typer.BadParameter("usage watcher lease was lost; restart the watcher")
            try:
                payload = _local_usage_import_payload(
                    store_dir=resolved_store_dir,
                    client=client,
                    codex_home=codex_home,
                    claude_home=claude_home,
                    opencode_home=opencode_home,
                    hermes_home=hermes_home,
                    openclaw_home=openclaw_home,
                    cursor_home=cursor_home,
                    limit_sessions=limit_sessions,
                    dry_run=False,
                    estimate_costs=estimate_costs,
                    refresh=refresh,
                )
            except Exception as exc:
                if once:
                    raise
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"[{timestamp}] scan_failed error={type(exc).__name__}; retrying in {interval_seconds:g}s",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                if json_output:
                    print(json.dumps(payload, sort_keys=True), flush=True)
                else:
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    totals = payload.get("usage_totals") if isinstance(payload.get("usage_totals"), dict) else {}
                    refreshed_events = int(payload.get("refreshed_events", 0) or 0)
                    repriced_events = int(payload.get("repriced_events", 0) or 0)
                    imported_events = int(payload.get("imported_events", 0) or 0)
                    migrated_events = int(payload.get("migrated_events", 0) or 0)
                    new_sessions = max(
                        0,
                        imported_events
                        - refreshed_events
                        - repriced_events
                        - migrated_events,
                    )
                    print(
                        f"[{timestamp}] imported={imported_events} "
                        f"new_sessions={new_sessions} "
                        f"refreshed={refreshed_events} "
                        f"repriced={repriced_events} "
                        f"observed_sessions={payload.get('observed_sessions', 0)} "
                        f"usage_sessions={payload.get('usage_sessions', 0)} "
                        f"usage_unavailable={payload.get('sessions_without_usage', 0)} "
                        f"saved_observations={payload.get('imported_session_observations', 0)} "
                        f"tokens={totals.get('input_tokens', 0)} in/{totals.get('output_tokens', 0)} out "
                        f"cache_create={totals.get('cache_creation_input_tokens', 0)} "
                        f"cache_read={totals.get('cache_read_input_tokens', 0)} "
                        f"incomplete_alias_migrations={payload.get('incomplete_alias_migrations', 0)}",
                        flush=True,
                    )
                    _print_evidence_refreshable_usage_warning(
                        payload,
                        prefix=f"[{timestamp}] ",
                    )
                # Maintenance rides only on a SUCCESSFUL scan tick; quiet
                # outcomes (disabled/no_store/current/cooldown) stay silent.
                # tick() is contractually fail-open, but this is a daemon:
                # if that contract ever regresses, a maintenance bug must
                # degrade to a rebuild_failed line, not kill the watcher.
                try:
                    rebuild_outcome = canonical_rebuild_policy.tick()
                except Exception as exc:  # noqa: BLE001 - belt-and-suspenders for the fail-open contract.
                    rebuild_outcome = {
                        "action": "failed",
                        "error": f"tick_escaped {type(exc).__name__}: {exc}"[:500],
                    }
                rebuild_action = rebuild_outcome.get("action")
                if rebuild_action == "rebuilt":
                    if json_output:
                        # --json's stdout contract is one JSON document per
                        # line; the rebuild notice must honor it too.
                        print(
                            json.dumps(
                                {"canonical_read_models": rebuild_outcome},
                                sort_keys=True,
                            ),
                            flush=True,
                        )
                    else:
                        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                        print(
                            f"[{timestamp}] canonical_read_models rebuilt "
                            f"tasks={rebuild_outcome.get('task_count')} "
                            f"usage_days={rebuild_outcome.get('usage_day_count')} "
                            f"built_through={rebuild_outcome.get('built_through_sequence')}",
                            flush=True,
                        )
                elif rebuild_action == "failed":
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{timestamp}] canonical_read_models rebuild_failed "
                        f"error={rebuild_outcome.get('error')}",
                        file=sys.stderr,
                        flush=True,
                    )
            if once:
                break
            if stop_requested.is_set():
                break
            if lease_id is not None and not health_store.heartbeat_watcher(lease_id):
                raise typer.BadParameter("usage watcher lease was lost; restart the watcher")
            if stop_requested.wait(interval_seconds):
                break
    finally:
        if lease_id is not None:
            health_store.release_watcher(lease_id)
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


@canonical_app.command("health")
def canonical_health(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the probe as JSON.")
    ] = False,
) -> None:
    """Read-only canonical store diagnostics.

    Presence, role, schema, canonical sequence, and per-projection
    built/stale state — the same flag-independent probe GET /health serves
    as ``canonical_store``. Never mutates the store; an unavailable store —
    including one whose directory cannot even be resolved — is an honest
    answer with exit 0, never an error exit.
    """

    from .canonical_read import CanonicalReadRuntime

    probe: dict[str, Any]
    try:
        resolution = resolve_store_dir(store_dir)
    except StoreResolutionError as exc:
        # The honest-answer contract covers the resolution step too: a
        # monitoring script keyed on "exit 0, parse the probe" must not
        # break because of its working directory.
        probe = {
            "available": False,
            "reason": "store_unresolved",
            "detail": str(exc)[:500],
        }
    else:
        if resolution.worktree_remapped:
            print(
                f"Claude worktree detected; using the owning project store: {resolution.path}",
                file=sys.stderr,
            )
        probe = CanonicalReadRuntime(resolution.path, enabled=False).health_probe()
    if json_output:
        print(json.dumps(probe, indent=2, sort_keys=True, default=str))
        return
    if not probe.get("available"):
        detail = probe.get("detail")
        suffix = f" ({detail})" if detail else ""
        print(f"canonical store unavailable: {probe.get('reason')}{suffix}")
        return
    store_block = probe.get("store") or {}
    print(
        f"store role={store_block.get('store_role')} "
        f"schema={store_block.get('schema_version')} "
        f"sequence={store_block.get('canonical_sequence')} "
        f"uuid={store_block.get('store_uuid')}"
    )
    for name, row in sorted((probe.get("projections") or {}).items()):
        state = row.get("state")
        if state == "never_built":
            print(f"projection {name}: never built")
        else:
            print(
                f"projection {name}: state={state} "
                f"built_through={row.get('built_through_sequence')} "
                f"pending_writes={row.get('pending_writes')} "
                f"stale={row.get('stale')}"
            )


def _canonical_cutover_cli_refusal(
    action: str,
    error: Exception,
    *,
    json_output: bool,
) -> NoReturn:
    payload = {
        "status": "refused",
        "action": action,
        "error_type": type(error).__name__,
        "error": str(error)[:1000],
    }
    if json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            f"canonical {action} refused: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
    raise typer.Exit(2)


@canonical_app.command("prepare-cutover")
def canonical_prepare_cutover(
    candidate: Annotated[
        Path,
        typer.Option("--candidate", help="Absolute path to the retained parity candidate."),
    ],
    parity_report: Annotated[
        Path,
        typer.Option("--parity-report", help="Absolute path to its parity-runner v3 report."),
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the preparation receipt as JSON.")
    ] = False,
) -> None:
    """Read-only gate binding one candidate to passed parity evidence."""

    from .canonical.cutover import prepare_cutover

    try:
        preparation = prepare_cutover(candidate, parity_report)
    except Exception as exc:  # noqa: BLE001 - one bounded, operator-facing refusal shape.
        _canonical_cutover_cli_refusal("cutover preparation", exc, json_output=json_output)
    payload = {"status": "prepared", "preparation": preparation.to_dict()}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(
        "canonical candidate prepared: "
        f"uuid={preparation.candidate_store_uuid} "
        f"sequence={preparation.canonical_sequence} "
        f"sha256={preparation.candidate_sha256}"
    )
    print(f"adapter rows to normalize: {preparation.adapter_rows_to_normalize}")


@canonical_app.command("promote")
def canonical_promote(
    candidate: Annotated[
        Path,
        typer.Option("--candidate", help="Absolute path to the retained parity candidate."),
    ],
    parity_report: Annotated[
        Path,
        typer.Option("--parity-report", help="Absolute path to its parity-runner v3 report."),
    ],
    receipt: Annotated[
        Path,
        typer.Option("--receipt", help="New absolute path for the durable promotion receipt."),
    ],
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    confirm_writers_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-writers-stopped",
            help="Acknowledge that every dashboard, watcher, and canonical MCP writer is stopped.",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the promotion and verification receipt as JSON.")
    ] = False,
) -> None:
    """Atomically install one parity-bound candidate over the stopped shadow."""

    from .canonical.cutover import (
        CutoverPostReplaceError,
        CutoverReceiptPersistenceError,
        CutoverVerificationError,
        prepare_cutover,
        promote_candidate,
    )

    if not confirm_writers_stopped:
        _canonical_cutover_cli_refusal(
            "promotion",
            ValueError("--confirm-writers-stopped is required"),
            json_output=json_output,
        )
    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    try:
        preparation = prepare_cutover(candidate, parity_report)
        result = promote_candidate(
            preparation,
            resolved_store_dir,
            receipt_path=receipt,
        )
    except CutoverPostReplaceError as exc:
        fallback_path = getattr(exc, "fallback_receipt_path", None)
        payload = {
            "status": (
                "promotion_replace_ambiguous"
                if exc.replacement_state == "ambiguous"
                else "promoted_post_replace_failed"
            ),
            "replacement_state": exc.replacement_state,
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "fallback_receipt_path": str(fallback_path) if fallback_path else None,
            "emergency_receipt": exc.receipt.to_dict(),
        }
        if json_output:
            print(json.dumps(payload, sort_keys=True))
        else:
            if exc.replacement_state == "ambiguous":
                print(
                    "CRITICAL: canonical promotion could not determine whether the live store was replaced.",
                    file=sys.stderr,
                )
            else:
                print(
                    "CRITICAL: canonical promotion replaced the live store, then a later step failed.",
                    file=sys.stderr,
                )
            if fallback_path:
                print(f"Emergency durable receipt: {fallback_path}", file=sys.stderr)
            print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        raise typer.Exit(2)
    except CutoverReceiptPersistenceError as exc:
        # Replacement already happened. Emit the complete in-memory receipt
        # even if both durable paths failed so recovery evidence is not hidden
        # behind a generic exit line.
        fallback_path = getattr(exc, "fallback_receipt_path", None)
        payload = {
            "status": "promoted_receipt_persistence_failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "requested_receipt_path": str(exc.receipt_path),
            "fallback_receipt_path": str(fallback_path) if fallback_path else None,
            "emergency_receipt": exc.receipt.to_dict(),
        }
        if json_output:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(
                "CRITICAL: canonical promotion was installed but receipt persistence failed.",
                file=sys.stderr,
            )
            if fallback_path:
                print(f"Emergency durable receipt: {fallback_path}", file=sys.stderr)
            print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        raise typer.Exit(2)
    except CutoverVerificationError as exc:
        payload = {
            "status": "promoted_verification_failed",
            "receipt_path": str(receipt),
            "receipt": exc.receipt.to_dict(),
            "verification": exc.verification.to_dict(),
        }
        if json_output:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(
                f"canonical promotion installed but verification failed; receipt: {receipt}",
                file=sys.stderr,
            )
            for error in exc.verification.errors:
                print(f"- {error}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as exc:  # noqa: BLE001 - fail closed before replacement.
        _canonical_cutover_cli_refusal("promotion", exc, json_output=json_output)
    receipt_path = Path(receipt).expanduser().resolve(strict=True)
    payload = {
        "status": "promoted",
        "receipt_path": str(receipt_path),
        **result.to_dict(),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(
        "canonical candidate promoted and verified: "
        f"uuid={result.receipt.promoted_store_uuid} "
        f"sequence={result.receipt.preparation.canonical_sequence}"
    )
    print(f"durable receipt: {receipt_path}")
    print(f"shadow backup: {result.receipt.backup_path}")


@canonical_app.command("verify-cutover")
def canonical_verify_cutover(
    receipt: Annotated[
        Path,
        typer.Option("--receipt", help="Absolute path to the durable promotion receipt."),
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit verification evidence as JSON.")
    ] = False,
) -> None:
    """Verify the exact promoted inode, projections, adapters, and backup."""

    from .canonical.cutover import load_promotion_receipt, verify_promotion

    try:
        promotion_receipt = load_promotion_receipt(receipt)
        verification = verify_promotion(promotion_receipt)
    except Exception as exc:  # noqa: BLE001 - corrupt/tampered receipts are refusals.
        _canonical_cutover_cli_refusal("cutover verification", exc, json_output=json_output)
    payload = {
        "status": "verified" if verification.ok else "blocked",
        "receipt_path": str(Path(receipt).expanduser()),
        "verification": verification.to_dict(),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif verification.ok:
        print(
            "canonical promotion verified: "
            f"uuid={verification.live_store_uuid} "
            f"sequence={verification.canonical_sequence}"
        )
    else:
        print("canonical promotion verification blocked:", file=sys.stderr)
        for error in verification.errors:
            print(f"- {error}", file=sys.stderr)
    if not verification.ok:
        raise typer.Exit(2)


@canonical_app.command("rollback")
def canonical_rollback(
    receipt: Annotated[
        Path,
        typer.Option("--receipt", help="Absolute path to the durable promotion receipt."),
    ],
    confirm_writers_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-writers-stopped",
            help="Acknowledge that every dashboard, watcher, reader, and canonical MCP writer is stopped.",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit rollback evidence as JSON.")
    ] = False,
) -> None:
    """Restore the exact shadow backup only if promoted bytes are unchanged."""

    from .canonical.cutover import (
        RollbackPostReplaceError,
        load_promotion_receipt,
        rollback_promotion,
    )

    if not confirm_writers_stopped:
        _canonical_cutover_cli_refusal(
            "rollback",
            ValueError("--confirm-writers-stopped is required"),
            json_output=json_output,
        )
    try:
        promotion_receipt = load_promotion_receipt(receipt)
        result = rollback_promotion(promotion_receipt)
    except RollbackPostReplaceError as exc:
        if exc.verification is not None:
            status = "rolled_back_verification_failed"
        elif exc.replacement_state == "installed":
            status = "rolled_back_post_replace_failed"
        else:
            status = "rollback_replace_ambiguous"
        payload = {
            "status": status,
            "replacement_state": exc.replacement_state,
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "rollback_receipt": (
                exc.receipt.to_dict() if exc.receipt is not None else None
            ),
            "verification": (
                exc.verification.to_dict()
                if exc.verification is not None
                else None
            ),
        }
        if json_output:
            print(json.dumps(payload, sort_keys=True))
        else:
            state = exc.replacement_state.upper()
            print(
                f"CRITICAL: canonical rollback replacement state is {state}.",
                file=sys.stderr,
            )
            print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        raise typer.Exit(2)
    except Exception as exc:  # noqa: BLE001 - never attempt an unsafe best-effort rollback.
        _canonical_cutover_cli_refusal("rollback", exc, json_output=json_output)
    payload = {"status": "rolled_back", **result.to_dict()}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(
        "canonical shadow restored and verified: "
        f"uuid={result.receipt.restored_store_uuid}"
    )
    print(f"retained backup: {result.receipt.backup_path}")


@canonical_app.command("rebuild-read-models")
def canonical_rebuild_read_models(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the rebuild result as JSON.")
    ] = False,
) -> None:
    """Rebuild rm_task_current and rm_usage_day from canonical rows.

    The sanctioned explicit rebuild entry (one write transaction; stamps
    projection_generations current) for operators and the phase-5 promote
    flow. The managed watcher performs the same maintenance automatically
    when the canonical live-write flag is on; read request paths never
    rebuild. This command never creates a store.
    """

    from .canonical.sqlite import LIVE_STORE_FILENAME, CanonicalStore

    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    try:
        store = CanonicalStore.open_live(resolved_store_dir)
    except FileNotFoundError:
        print(
            f"no canonical store at {resolved_store_dir / LIVE_STORE_FILENAME}",
            file=sys.stderr,
        )
        raise typer.Exit(2)
    except Exception as exc:  # noqa: BLE001 - the fail-closed open's refusal is the diagnostic.
        print(f"canonical store refused: {exc}", file=sys.stderr)
        raise typer.Exit(2)
    try:
        result = store.repository().rebuild_minimal_read_models()
    except Exception as exc:  # noqa: BLE001 - one operator-facing failure shape: a line on stderr and exit 2.
        print(f"canonical rebuild failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise typer.Exit(2)
    finally:
        store.close()
    if json_output:
        print(
            json.dumps(
                {"store_dir": str(resolved_store_dir), **result},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            f"rebuilt read models: tasks={result['task_count']} "
            f"usage_days={result['usage_day_count']} "
            f"built_through={result['built_through_sequence']}"
        )


@canonical_app.command("rebuild-store")
def canonical_rebuild_store(
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_STORE_DIR_HELP),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the rebuild result as JSON.")
    ] = False,
) -> None:
    """Build a fresh live canonical store (chronicle.sqlite3) from events.jsonl.

    Reads the append-only events.jsonl truth and materializes the SQLite read
    index — sessions, tasks, and the rm_task_current + rm_usage_day read
    models — then installs it at the reserved live name so the fast read path
    can serve it. Any existing store is replaced: the JSONL ledger is the
    authority and the store is a rebuildable index over it.

    Unlike ``rebuild-read-models`` (which refreshes projections on an EXISTING
    store), this creates the store from the ledger. It is a local/dev rebuild,
    NOT the authoritative cutover — that stays ``canonical promote``.
    """

    from .canonical.rebuild import rebuild_live_store_from_events

    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    try:
        report = rebuild_live_store_from_events(resolved_store_dir)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise typer.Exit(2)
    except Exception as exc:  # noqa: BLE001 - one operator-facing failure shape: stderr + exit 2.
        print(f"canonical rebuild failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise typer.Exit(2)
    if json_output:
        print(
            json.dumps(
                {
                    "store_path": str(report.store_path),
                    "parsed_events": report.parsed_events,
                    "session_count": report.session_count,
                    "task_count": report.task_count,
                    "usage_day_count": report.usage_day_count,
                    "issue_count": report.issue_count,
                    "canonical_sequence": report.canonical_sequence,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    print(f"rebuilt canonical store: {report.store_path}")
    print(
        f"  sessions={report.session_count} tasks={report.task_count} "
        f"usage_days={report.usage_day_count}"
    )
    print(
        f"  parsed_events={report.parsed_events} not_imported={report.issue_count} "
        f"canonical_sequence={report.canonical_sequence}"
    )
    if report.issue_count:
        print(
            f"  note: {report.issue_count} ledger lines were not imported (e.g. "
            "events without a source_namespace_fingerprint, such as agentacct's "
            "own section/check records); usage rows are unaffected."
        )


@usage_app.command("health")
def usage_health(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Show per-source scan receipts and continuous watcher health."""

    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    snapshot = IngestionHealthStore(resolved_store_dir).snapshot()
    if json_output:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return
    print(f"Ingestion: {snapshot.get('state', 'unknown')}")
    watcher = snapshot.get("watcher") if isinstance(snapshot.get("watcher"), dict) else {}
    print(f"Watcher: {watcher.get('state', 'not_configured')}")
    for source in snapshot.get("sources") or []:
        if not isinstance(source, dict):
            continue
        print(
            f"{source.get('source')}: {source.get('state')} "
            f"parsed={source.get('parsed', 0)} errors={source.get('error_count', 0)}"
        )
    for issue in snapshot.get("issues") or []:
        if isinstance(issue, dict):
            print(f"Action: {issue.get('action')}")


@usage_app.command("repair-dead-scans")
def usage_repair_dead_scans(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the repair receipt and resulting health as JSON.")
    ] = False,
) -> None:
    """Retire scan receipts only when their recorded process is gone.

    This is an explicit recovery command: ordinary health reads never mutate
    state. It probes PIDs with signal 0 and removes only records whose process
    is definitively absent; live, invalid, inaccessible, and concurrently
    changed records are preserved.
    """

    resolved_store_dir = _resolve_cli_store_dir(store_dir).path
    health_store = IngestionHealthStore(resolved_store_dir)
    removed_scan_ids = health_store.repair_dead_active_scans()
    snapshot = health_store.snapshot()
    payload = {
        "store_dir": str(resolved_store_dir),
        "removed_count": len(removed_scan_ids),
        "removed_scan_ids": list(removed_scan_ids),
        "health": snapshot,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Retired dead scan receipts: {len(removed_scan_ids)}")
    print(f"Ingestion: {snapshot.get('state', 'unknown')}")


@usage_app.command("merge-store")
def usage_merge_store(
    from_store: Annotated[
        Path,
        typer.Option("--from", help="Source store state directory to copy events FROM (read-only; never modified)."),
    ],
    into_store: Annotated[
        Path,
        typer.Option("--into", help="Target store state directory to copy events INTO (additive; existing rows are never rewritten)."),
    ],
    kind: Annotated[
        str,
        typer.Option(help="Which events to merge: 'mcp' (default; work/context events only — sections, machine checks, context attaches) or 'all' (also client-reported usage rows). 'mcp' is the primary use: usage truth is normally imported globally from client logs, so re-merging usage rows risks double-counting; 'all' logically dedups usage rows to stay safe."),
    ] = "mcp",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what would be merged and write nothing.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Merge events from one store into another, dedup-safe and additive-only.

    Copies events verbatim (each event keeps its own event_id, created_at, and
    provenance/trust markers), skipping any event id already present in the
    target. The narrow exception is an unlike trusted usage or observation
    truth collision: the incoming truth row receives a deterministic reminted
    id so neither fact is lost. Nothing already in the target is deleted or
    rewritten. Preserving ordinary section event ids is what lets the
    client-log-evidence join re-link merged work context to the usage sessions
    that reference those ids in the target store.

    Defaults to --kind mcp: merging WORK CONTEXT is the point, and usage truth
    is normally imported globally from the client's own logs. --kind all also
    copies usage rows, deduplicating them by their LOGICAL identity (client,
    base session, lane) rather than event_id (which is minted fresh per import),
    so the same session imported into both stores is never double-counted.
    """
    if kind not in store_merge.MERGE_KINDS:
        raise typer.BadParameter("--kind must be one of: all, mcp")
    from_path = from_store.expanduser()
    into_path = into_store.expanduser()
    if not from_path.exists():
        print(f"Source store does not exist: {from_path}", file=sys.stderr)
        raise typer.Exit(2)
    if from_path.resolve() == into_path.resolve():
        print("Source and target stores must be different directories.", file=sys.stderr)
        raise typer.Exit(2)

    # Read the source read-only (create=False: never mkdir a source that is
    # missing its runs dir). The target is created if absent.
    source_service = SentinelService(from_path, create=False)
    source_events = source_service.list_all_events()
    target_service = SentinelService(into_path, create=not dry_run)
    added: list[dict[str, object]] = []
    if dry_run:
        target_events = target_service.list_all_events() if into_path.exists() else []
        target_ids = {
            event_id
            for event in target_events
            if isinstance((event_id := event.get("event_id")), str) and event_id
        }
        plan = store_merge.plan_store_merge(
            source_events,
            target_ids,
            kind=kind,
            target_usage_identities=store_merge.usage_row_identities(target_events),
        )
        plan["target_events_before"] = len(target_ids)
    else:
        plan, merged = target_service.merge_events_preserving_identity(
            source_events,
            kind=kind,
        )
        added = list(merged)

    payload = {
        "from": str(from_path),
        "into": str(into_path),
        "kind": kind,
        "dry_run": dry_run,
        "source_events": plan["source_events"],
        "target_events_before": plan["target_events_before"],
        "added": len(added) if not dry_run else plan["add_count"],
        "skipped_existing": plan["skipped_existing_count"],
        "skipped_duplicate_usage": plan["skipped_duplicate_usage_count"],
        "skipped_usage_namespace_ambiguous": plan[
            "skipped_usage_namespace_ambiguous_count"
        ],
        "preserved_cross_namespace_usage": plan[
            "preserved_cross_namespace_usage_count"
        ],
        "preserved_ambiguous_usage_namespace": plan[
            "preserved_ambiguous_usage_namespace_count"
        ],
        "skipped_duplicate_observation": plan[
            "skipped_duplicate_observation_count"
        ],
        "observation_conflict_rows": plan["observation_conflict_count"],
        "preserved_observation_conflict_rows": plan[
            "preserved_observation_conflict_count"
        ],
        "reminted_truth_event_id_conflicts": plan[
            "reminted_truth_event_id_conflicts"
        ],
        "observation_non_conflict_skips": plan[
            "observation_non_conflict_skip_count"
        ],
        "filtered_out": plan["filtered_out_count"],
        "no_event_id": plan["no_event_id_count"],
        "added_by_type": plan["added_by_type"],
        "skipped_existing_by_type": plan["skipped_existing_by_type"],
        "skipped_duplicate_usage_by_type": plan["skipped_duplicate_usage_by_type"],
        "skipped_usage_namespace_ambiguous_by_type": plan[
            "skipped_usage_namespace_ambiguous_by_type"
        ],
        "skipped_duplicate_observation_by_type": plan[
            "skipped_duplicate_observation_by_type"
        ],
        "skipped_observation_by_reason": plan["skipped_observation_by_reason"],
        "observation_reducer_diagnostics": plan[
            "observation_reducer_diagnostics"
        ],
        "filtered_out_by_type": plan["filtered_out_by_type"],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    action = "Would merge" if dry_run else "Merged"
    verb = "Would add" if dry_run else "Added"
    print(f"{action} events from {from_path} into {into_path} (kind={kind}).")
    print(
        f"{verb} {payload['added']} new event(s); "
        f"skipped {payload['skipped_existing']} already-present event(s)"
        + (
            f"; skipped {payload['skipped_duplicate_usage']} duplicate usage row(s)"
            if payload["skipped_duplicate_usage"]
            else ""
        )
        + (
            f"; quarantined {payload['skipped_usage_namespace_ambiguous']} usage row(s) with missing-vs-explicit source-home provenance"
            if payload["skipped_usage_namespace_ambiguous"]
            else ""
        )
        + (
            f"; preserved {payload['preserved_ambiguous_usage_namespace']} usage row(s) in missing-vs-explicit source-home quarantine"
            if payload["preserved_ambiguous_usage_namespace"]
            else ""
        )
        + (
            f"; preserved {payload['preserved_cross_namespace_usage']} usage row(s) from distinct explicit source homes for downstream reconciliation"
            if payload["preserved_cross_namespace_usage"]
            else ""
        )
        + (
            f"; reminted {payload['reminted_truth_event_id_conflicts']} trusted truth event id collision(s)"
            if payload["reminted_truth_event_id_conflicts"]
            else ""
        )
        + (
            f"; skipped {payload['skipped_duplicate_observation']} non-authoritative session observation(s) "
            f"({payload['observation_non_conflict_skips']} idempotent/historical, "
            f"{payload['observation_conflict_rows']} quarantined conflict row(s))"
            if payload["skipped_duplicate_observation"]
            else ""
        )
        + (
            f"; filtered out {payload['filtered_out']} local usage/observation row(s)"
            if kind == "mcp"
            else ""
        )
        + "."
    )
    if payload["added_by_type"]:
        print("By event type:")
        for event_type, count in sorted(payload["added_by_type"].items(), key=lambda item: (-item[1], item[0])):
            print(f"  +{count}  {event_type}")
    if dry_run:
        print("Dry run: no events written. Re-run without --dry-run to apply.")
    else:
        print(f"Local API: agentacct serve --store-dir {shlex.quote(str(into_path))}")


@usage_app.command("discover-sources")
def usage_discover_sources(
    codex_home: Annotated[Optional[Path], typer.Option(help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.")] = None,
    claude_home: Annotated[Optional[Path], typer.Option(help="Claude Code home directory. Defaults to CLAUDE_CONFIG_DIR, XDG config, or ~/.claude.")] = None,
    opencode_home: Annotated[Optional[Path], typer.Option(help="OpenCode data/export directory. Defaults to OPENCODE_DATA_DIR or ~/.local/share/opencode.")] = None,
    hermes_home: Annotated[Optional[Path], typer.Option(help="Hermes home directory. Defaults to HERMES_HOME or ~/.hermes.")] = None,
    openclaw_home: Annotated[Optional[Path], typer.Option(help="OpenClaw home directory. Defaults to OPENCLAW_DIR or known OpenClaw roots.")] = None,
    cursor_home: Annotated[Optional[Path], typer.Option(help="Cursor application-support root. Defaults to ~/Library/Application Support/Cursor; only User/globalStorage/state.vscdb is inspected.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Detect local coding-agent usage stores before importing anything."""

    sources = discover_usage_sources(
        codex_home=codex_home,
        claude_home=claude_home,
        opencode_home=opencode_home,
        hermes_home=hermes_home,
        openclaw_home=openclaw_home,
        cursor_home=cursor_home,
    )
    payload = {"sources": [source.to_dict() for source in sources]}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    table = Table(title="Detected local usage sources")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Evidence")
    table.add_column("Sessions")
    table.add_column("Files")
    table.add_column("Importer")
    table.add_column("Confidence")
    for source in sources:
        sessions = "" if source.session_count is None else str(source.session_count)
        confidence = f"usage={source.usage_confidence}; cost={source.cost_confidence}"
        importer = (source.importer or "pending") if source.status == "found" else ""
        table.add_row(
            source.display_name,
            source.status,
            source.evidence,
            sessions,
            str(source.file_count),
            importer,
            confidence,
        )
    console.print(table)
    console.print(
        "Discovery is read-only: no API keys or provider calls; transcript content is never retained or emitted."
    )


@usage_app.command("truth-table")
def usage_truth_table_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Show what each integration path can and cannot prove about usage and cost."""
    rows = usage_truth_table()
    if json_output:
        print(json.dumps({"integrations": rows}, indent=2, sort_keys=True))
        return
    table = Table(title="agentacct usage/cost truth table")
    table.add_column("Integration")
    table.add_column("Evidence")
    table.add_column("Freshness")
    table.add_column("Usage")
    table.add_column("Cost")
    table.add_column("Hard budget basis")
    for row in rows:
        table.add_row(
            str(row["integration"]),
            str(row["tier"]),
            str(row["update_timing"]),
            str(row["usage_confidence"]),
            str(row["cost_confidence"]),
            str(row["hard_budget_basis"]),
        )
    console.print(table)
    console.print("Details:")
    for row in rows:
        console.print(f"- {row['integration']}: {row['setup_path']}")


_SERVE_PORT_FALLBACK_SPAN = 20
"""How many ports past the default the dashboard probes before giving up."""


def _probe_port_free(host: str, port: int) -> bool:
    """Return True if ``(host, port)`` can be bound right now.

    Mirrors uvicorn's own socket setup (``SO_REUSEADDR``) so the probe reflects
    what the server will attempt a moment later. There is an unavoidable TOCTOU
    window between this check and uvicorn's bind, but for a localhost dashboard
    the practical risk is negligible.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _select_serve_port(
    host: str,
    port: int,
    *,
    allow_fallback: bool,
    max_offset: int = _SERVE_PORT_FALLBACK_SPAN,
) -> int:
    """Pick a bindable port for the local dashboard.

    When ``allow_fallback`` is False (the user passed an explicit ``--port``) the
    requested port must be free or an ``OSError`` is raised so the caller can
    fail with an actionable message. When True (the default-port path) a busy
    port advances through ``port+1 .. port+max_offset`` and the first free port is
    returned; ``OSError`` is raised only if the whole range is occupied.
    """
    if _probe_port_free(host, port):
        return port
    if not allow_fallback:
        raise OSError(errno.EADDRINUSE, f"port {port} is already in use")
    for candidate in range(port + 1, port + max_offset + 1):
        if _probe_port_free(host, candidate):
            return candidate
    raise OSError(errno.EADDRINUSE, f"no free port in range {port}-{port + max_offset}")


@app.command("now")
def now(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    window: Annotated[
        str,
        typer.Option(help="Window for the by-client/by-model breakdown: today, 7d, 30d, or all."),
    ] = "7d",
    client: Annotated[
        Optional[str],
        typer.Option(help="Filter to one client (e.g. codex, claude-code); 'all' or omit for every client."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Current usage & cost snapshot — calendar-window tokens/cost, by client and model.

    Reads the authoritative local event log (no credentials, no API calls). Costs
    are agentacct's token-based ESTIMATES, not provider billing; a window that is
    not fully priced shows a partial ``~$`` subtotal. See ``agentacct limits`` for
    provider rate-limit windows.
    """
    if window not in NOW_WINDOW_ALIASES:
        raise typer.BadParameter("--window must be one of: today, 7d, 30d, all")
    # 'all' / omitted → no client filter (matches `limits` and the dashboard).
    effective_client = None if client in (None, "all") else client

    resolved_store_dir = _resolve_read_cli_store_dir(store_dir).path
    service = SentinelService(resolved_store_dir, create=False)
    events = service.list_all_events()

    # The shared snapshot layer (usage_snapshot) maps events → usage records
    # (model_usage only; session / diagnostic / work events excluded) and windows
    # them via the cube's days=N path — the exact computation `agentacct now`, the
    # dashboard's usage summary, and the TUI all share, so none can disagree.
    snapshot = build_usage_snapshot(events, client=effective_client, breakdown_window=window)

    def _limits_payload() -> list[dict[str, Any]]:
        try:
            return [
                limit_json_entry(e)
                for e in latest_limit_events(events, client=effective_client)
            ]
        except Exception:  # noqa: BLE001 - limits are best-effort; never break `now --json`.
            return []

    if json_output:
        payload = {
            "as_of": snapshot.as_of,
            "generated_at": snapshot.generated_at,
            "event_count": snapshot.event_count,
            "usage_record_count": snapshot.usage_record_count,
            "client_filter": snapshot.client_filter,
            "windows": [{"window": w.label, "totals": w.totals} for w in snapshot.windows],
            "breakdown_window": snapshot.breakdown_window,
            "by_client": snapshot.by_client,
            "by_model": snapshot.by_model,
            "limits": _limits_payload(),
        }
        print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False, default=str))
        return

    console.print(
        "agentacct now — local event log; costs are token-based estimates, not billing."
    )
    if snapshot.as_of is not None:
        console.print(
            f"as of {humanize_seconds(snapshot.generated_at - snapshot.as_of)} ago · "
            f"{snapshot.usage_record_count} usage records\n"
        )
    elif snapshot.usage_record_count:
        console.print(f"{snapshot.usage_record_count} usage records · newest timestamp unknown\n")
    else:
        console.print("")

    if snapshot.usage_record_count == 0:
        if effective_client is not None:
            console.print(f"No usage recorded for client '{effective_client}'.")
        else:
            console.print("No usage recorded yet.")
        console.print(
            "  • Import local usage: `agentacct usage import-local` (or `agentacct usage watch`)."
        )
        return

    windows_table = Table(title=None)
    windows_table.add_column("window")
    windows_table.add_column("tokens", justify="right")
    windows_table.add_column("est. cost", justify="right")
    windows_table.add_column("sessions", justify="right")
    for entry in snapshot.windows:
        totals = entry.totals
        windows_table.add_row(
            entry.label,
            format_tokens(totals.get("total_tokens_including_cached")),
            cost_text(totals),
            format_tokens(totals.get("sessions")),
        )
    console.print(windows_table)
    console.print("[dim]~ cost = partial (some usage unpriced or held); costs are estimates, not billing.[/dim]")

    by_client = snapshot.by_client
    if by_client:
        client_table = Table(title=f"by client · {snapshot.breakdown_window}")
        client_table.add_column("client")
        client_table.add_column("tokens", justify="right")
        client_table.add_column("est. cost", justify="right")
        client_table.add_column("sessions", justify="right")
        for row in sorted(by_client, key=lambda r: -(r.get("total_tokens_including_cached") or 0)):
            client_table.add_row(
                str(row.get("client")),
                format_tokens(row.get("total_tokens_including_cached")),
                cost_text(row),
                format_tokens(row.get("sessions")),
            )
        console.print(client_table)

    by_model = snapshot.by_model
    if by_model:
        model_table = Table(title=f"top models · {snapshot.breakdown_window}")
        model_table.add_column("model")
        model_table.add_column("client")
        model_table.add_column("tokens", justify="right")
        model_table.add_column("est. cost", justify="right")
        top_models = sorted(by_model, key=lambda r: -(r.get("total_tokens_including_cached") or 0))[:8]
        for row in top_models:
            model_table.add_row(
                str(row.get("model") or "—"),
                str(row.get("client") or ""),
                format_tokens(row.get("total_tokens_including_cached")),
                cost_text(row),
            )
        console.print(model_table)

    teaser = limit_teaser_lines(events, client=effective_client)
    if teaser:
        console.print("\nlimits (provider-reported; run `agentacct limits` for detail):")
        for line in teaser:
            console.print(f"  {line}")


@app.command("limits")
def limits(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    client: Annotated[
        Optional[str],
        typer.Option(help="Filter to one client: all (default), codex, or claude-code."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Show provider-reported usage limits (5-hour / weekly windows).

    Read passively from local files — no credentials, no API calls. Codex limits
    come from ~/.codex session files; Claude limits from the Claude desktop app's
    plan-usage history, or — for terminal-CLI users — a statusLine hook
    (``agentacct onboard`` installs it). Claude limits are Pro/Max only. Populate
    them by using your agents, or by running ``agentacct usage watch``. Percentages
    are the provider's own reported utilization; reset times are shown when the
    provider records them (Codex and the Claude statusLine).
    """

    # Normalize/validate the client selector the same way the rest of the CLI
    # does: None/"all" means no filter; only the two clients that emit limits are
    # valid. (A bare unknown value must error, not silently render "no data".)
    if client is None or client == "all":
        effective_client: str | None = None
    elif client in ("codex", "claude-code"):
        effective_client = client
    else:
        raise typer.BadParameter("--client must be one of: all, codex, claude-code")

    resolved_store_dir = _resolve_read_cli_store_dir(store_dir).path
    service = SentinelService(resolved_store_dir, create=False)
    snapshots = latest_limit_events(service.list_all_events(), client=effective_client)

    if json_output:
        print(
            json.dumps(
                {"limits": [limit_json_entry(e) for e in snapshots]},
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        )
        return

    if not snapshots:
        console.print("No provider limit data recorded yet.")
        console.print("  • Codex: run a codex session, or `agentacct usage watch`.")
        console.print(
            "  • Claude (desktop app, Pro/Max): use Claude, or `agentacct usage watch`."
        )
        console.print(
            "  • Claude (terminal CLI, Pro/Max): `agentacct onboard` installs a "
            "statusLine that captures limits while you code."
        )
        return

    now = time.time()
    console.print(
        "Provider-reported usage limits — local files, no API. Provider estimates, not billing.\n"
    )
    for event in snapshots:
        metadata = event.get("metadata") or {}
        event_client = metadata.get("client") or "?"
        header = str(event_client)
        origin_label = ORIGIN_LABELS.get(str(metadata.get("origin") or ""))
        if origin_label:
            header += f" ({origin_label})"
        plan = metadata.get("plan_type")
        if plan:
            header += f"  plan: {plan}"
        org = metadata.get("org")
        if org:
            header += f"  org: {str(org)[:8]}"
        captured = metadata.get("captured_at")
        if not isinstance(captured, (int, float)) or isinstance(captured, bool):
            created = event.get("created_at")
            captured = created if isinstance(created, (int, float)) else None
        if isinstance(captured, (int, float)):
            header += f"  (as of {humanize_seconds(now - captured)} ago)"
        console.print(header)
        windows = metadata.get("windows")
        for window in windows if isinstance(windows, list) else []:
            if not isinstance(window, Mapping):
                continue
            used = window.get("used_percent")
            used_value = float(used) if isinstance(used, (int, float)) and not isinstance(used, bool) else 0.0
            line = f"  {window_label(window):>7}  {usage_bar(used_value)}  {used_value:5.1f}%"
            resets_at = window.get("resets_at")
            if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool) and resets_at > 0:
                delta = resets_at - now
                line += (
                    f"  · resets in {humanize_seconds(delta)}"
                    if delta > 0
                    else "  · resets now"
                )
            console.print(line)
        credits = metadata.get("credits")
        if isinstance(credits, Mapping) and credits.get("has_credits"):
            console.print(f"  credits: {credits.get('balance')}")
        reached = metadata.get("reached_type")
        if reached:
            console.print(f"  ⚠ limit reached: {reached}")
        console.print()


@app.command("tui")
def tui(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    window: Annotated[
        str,
        typer.Option(help="Initial breakdown window: today, 7d, 30d, or all (cycle with 'w')."),
    ] = "7d",
    client: Annotated[
        Optional[str],
        typer.Option(help="Filter to one client (e.g. codex, claude-code); 'all' or omit for every client."),
    ] = None,
    refresh: Annotated[
        float,
        typer.Option(help="Seconds between event-log polls (minimum 1)."),
    ] = 5.0,
) -> None:
    """Live terminal dashboard — usage, cost, and provider rate limits, in place.

    A full-screen view over the same authoritative local event log as
    ``agentacct now`` / ``agentacct limits`` (no credentials, no API calls). Keys:
    ``r`` refresh now, ``w`` cycle the breakdown window, ``s`` open the sessions
    drill-down (a session's steps and their check results), ``u`` open the usage
    screen (a per-day/-week token & cost time series with by-model detail), ``q``
    quit. Needs an interactive terminal.
    """
    if window not in NOW_WINDOW_ALIASES:
        raise typer.BadParameter("--window must be one of: today, 7d, 30d, all")
    # A full-screen TUI needs a real terminal; in a pipe / CI / captured stdout
    # there is nothing to drive it, so fail with a clear pointer instead of
    # hanging or crashing.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print(
            "agentacct tui needs an interactive terminal. For scripting, use "
            "`agentacct now --json` or `agentacct limits --json`."
        )
        raise typer.Exit(1)
    effective_client = None if client in (None, "all") else client
    resolved_store_dir = _resolve_read_cli_store_dir(store_dir).path

    try:
        from .tui import AgentAcctTUI
    except ImportError:
        console.print(
            "The TUI needs the optional 'textual' package. Install it with: "
            "pip install textual"
        )
        raise typer.Exit(1)

    AgentAcctTUI(
        store_dir=resolved_store_dir,
        client=effective_client,
        window_token=window,
        refresh_seconds=refresh,
    ).run()


@app.command("serve")
def serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Bind host. Default is 127.0.0.1 for local dashboard safety.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port for the local dashboard and event API.")] = 8765,
    store_dir: Annotated[
        Optional[Path],
        typer.Option(help=_DASHBOARD_STORE_DIR_HELP),
    ] = None,
    allow_host: Annotated[Optional[list[str]], typer.Option("--allow-host", help=_ALLOW_HOST_HELP)] = None,
) -> None:
    """Serve the local JSON API on localhost with local usage discovery enabled.

    The default port (8765) auto-advances to the next free port when it is busy,
    so the dashboard never fails to start just because a port is taken. An
    explicit ``--port`` is honored strictly: if that exact port is occupied the
    command fails rather than silently moving.
    """
    import uvicorn

    if host not in {"127.0.0.1", "localhost"}:
        console.print("Refusing non-local bind by default. Do not expose the dashboard without authentication.")
        raise typer.Exit(1)
    # Resolve BEFORE uvicorn.run so a missing store is an actionable exit(2),
    # not a server crash mid-startup.
    resolution = _resolve_dashboard_cli_store_dir(store_dir)
    effective_store_dir = resolution.path
    # Only auto-advance when the port came from the default; an explicit --port
    # is a specific request and must fail loudly instead of moving. Compare the
    # click ParameterSource by name -- typer's runtime enum is not identical to
    # click.core's, so identity/value comparisons are unreliable across versions.
    port_source = ctx.get_parameter_source("port")
    port_explicitly_set = getattr(port_source, "name", "DEFAULT") != "DEFAULT"
    try:
        bound_port = _select_serve_port(host, port, allow_fallback=not port_explicitly_set)
    except OSError:
        if port_explicitly_set:
            console.print(
                f"Port {port} is already in use. Pass a free --port, or omit --port to let "
                "agentacct pick the next free port automatically."
            )
        else:
            console.print(
                f"Ports {port}-{port + _SERVE_PORT_FALLBACK_SPAN} are all in use. "
                "Free one up or pass an explicit --port."
            )
        raise typer.Exit(1)
    if bound_port != port:
        console.print(f"Port {port} was busy; dashboard on http://{host}:{bound_port}")
    console.print(f"Starting the agentacct local API (JSON): http://{host}:{bound_port}")
    if resolution.source == "global":
        console.print("Dashboard scope: All projects (machine-wide store).")
        _warn_dashboard_mcp_store_shadow(effective_store_dir)
    elif resolution.source == "project":
        console.print("Dashboard scope: current workspace.")
    else:
        console.print("Dashboard scope: explicit store override.")
    console.print(
        "Local usage scan: enabled for this localhost dashboard; agentacct reads implemented local agent usage paths "
        "and imports only summarized usage rows. Use `agentacct api serve` for an API server with local usage discovery disabled."
    )
    # Native-shell handshake: a per-boot bearer token published through the
    # 0600 discovery file next to the store. Claimed AFTER the bind port is
    # chosen so readers always see the real port; first-alive-writer-wins (a
    # second serve against the same store leaves a live owner's slot alone);
    # removed on shutdown behind a pid gate + lock so a dying old server never
    # deletes a fresh server's file.
    import secrets as _secrets
    import threading as _threading

    from .glance import claim_discovery_file, remove_discovery_file, run_discovery_heartbeat

    v1_token = _secrets.token_urlsafe(32)
    v1_version_string = _usage_importer_version()
    discovery_path = claim_discovery_file(
        effective_store_dir,
        host=host,
        port=bound_port,
        token=v1_token,
        version=v1_version_string,
    )
    if discovery_path is not None:
        console.print(f"Native-shell API (/v1): discovery file {discovery_path}")
    else:
        console.print(
            "Native-shell API (/v1): another agentacct server already publishes the discovery "
            "file for this store; this instance serves /v1 unpublished (it re-claims the slot "
            "automatically if that server goes away)."
        )
    # The re-claim heartbeat closes the restart-drain stranding: a serve that
    # started unpublished (old server still dying) takes the slot over within
    # one interval of it freeing up; also heals SIGKILL-stale files.
    heartbeat_stop = _threading.Event()
    heartbeat = _threading.Thread(
        target=run_discovery_heartbeat,
        kwargs={
            "store_dir": effective_store_dir,
            "host": host,
            "port": bound_port,
            "token": v1_token,
            "version": v1_version_string,
            "stop": heartbeat_stop,
        },
        name="agentacct-discovery-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    try:
        uvicorn.run(
            create_local_api_app(
                store_dir=effective_store_dir,
                usage_discovery=UsageDiscoveryConfig.real_home(),
                extra_allowed_hosts=tuple(allow_host or ()),
                v1_auth_token=v1_token,
            ),
            host=host,
            port=bound_port,
            log_level="info",
        )
    finally:
        # Stop the heartbeat BEFORE removing so it cannot re-publish a file
        # this shutdown just deleted.
        heartbeat_stop.set()
        heartbeat.join(timeout=5)
        remove_discovery_file(effective_store_dir)


@api_app.command("serve")
def api_serve(
    host: Annotated[str, typer.Option(help="Bind host. Default is localhost for safety.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8789,
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    allow_host: Annotated[Optional[list[str]], typer.Option("--allow-host", help=_ALLOW_HOST_HELP)] = None,
) -> None:
    """Serve the local JSON/event API for sidecar/MCP integrations.

    This local API is unauthenticated, intended for trusted localhost clients,
    and exposes report/outcome/value/event primitives. It does not call paid
    judge APIs.
    """
    import uvicorn

    if host not in {"127.0.0.1", "localhost"}:
        console.print("Refusing non-local bind by default. Use a reverse proxy/lab only after adding auth.")
        raise typer.Exit(1)
    effective_store_dir = _resolve_cli_store_dir(store_dir).path
    uvicorn.run(
        create_local_api_app(store_dir=effective_store_dir, extra_allowed_hosts=tuple(allow_host or ())),
        host=host,
        port=port,
        log_level="info",
    )


@cost_app.command("proxy")
def cost_proxy(
    host: Annotated[str, typer.Option(help="Bind host for the local dry-run proxy.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port for the local dry-run proxy.")] = 8787,
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    allow_host: Annotated[Optional[list[str]], typer.Option("--allow-host", help=_ALLOW_HOST_HELP)] = None,
    max_total_usd: Annotated[Optional[float], typer.Option(help="Block if best-known billable ledger total would exceed this budget.")] = None,
    max_total_tokens: Annotated[Optional[int], typer.Option(help="Block if estimated total tokens would exceed this budget.")] = None,
    max_input_tokens: Annotated[Optional[int], typer.Option(help="Block if estimated input tokens would exceed this budget.")] = None,
    max_output_tokens: Annotated[Optional[int], typer.Option(help="Block if estimated output tokens would exceed this budget.")] = None,
    enable_forwarding: Annotated[bool, typer.Option(help="Actually forward allowlisted provider requests upstream. Default is dry-run only.")] = False,
    forward_provider: Annotated[list[str] | None, typer.Option(help="Provider allowed for forwarding. Supports openrouter, openai, and deepseek.")] = None,
    dry_run_only: Annotated[bool, typer.Option(help="Validate forwarding options and exit without starting a server.")] = False,
) -> None:
    """Start the v0 cost proxy. Forwarding is disabled unless explicitly enabled."""
    if host not in {"127.0.0.1", "localhost"}:
        console.print("Refusing non-local bind by default. Do not expose the cost proxy without authentication.")
        raise typer.Exit(1)
    allowed_forward_providers = set(forward_provider or [])
    if enable_forwarding:
        supported_forward_providers = {"openrouter", "openai", "deepseek"}
        unsupported = sorted(allowed_forward_providers - supported_forward_providers)
        if unsupported:
            raise typer.BadParameter(f"unsupported forward provider(s): {', '.join(unsupported)}")
        if not allowed_forward_providers:
            raise typer.BadParameter("forwarding requires at least one --forward-provider")
        if max_total_usd is None:
            raise typer.BadParameter("forwarding requires --max-total-usd local budget cap")
        # New AGENTACCT_* names win; pre-rename AGENT_CHRONICLE_* / AGENT_SENTINEL_* aliases
        # are accepted silently.
        openrouter_api_key = read_env_alias("AGENTACCT_OPENROUTER_API_KEY")
        openai_api_key = read_env_alias("AGENTACCT_OPENAI_API_KEY")
        deepseek_api_key = read_env_alias("AGENTACCT_DEEPSEEK_API_KEY")
        if "openrouter" in allowed_forward_providers and not openrouter_api_key:
            raise typer.BadParameter("OpenRouter forwarding requires AGENTACCT_OPENROUTER_API_KEY")
        if "openrouter" in allowed_forward_providers and openrouter_api_key and not openrouter_api_key.startswith("sk-or-v1-"):
            raise typer.BadParameter("AGENTACCT_OPENROUTER_API_KEY does not look like a full OpenRouter key")
        if "openai" in allowed_forward_providers and not openai_api_key:
            raise typer.BadParameter("OpenAI forwarding requires AGENTACCT_OPENAI_API_KEY")
        if "deepseek" in allowed_forward_providers and not deepseek_api_key:
            raise typer.BadParameter("DeepSeek forwarding requires AGENTACCT_DEEPSEEK_API_KEY")
    else:
        openrouter_api_key = None
        openai_api_key = None
        deepseek_api_key = None
    if dry_run_only:
        console.print("Dry-run-only validation passed; server not started.")
        return
    # Resolve after the dry-run-only shortcut (which touches no store) and
    # before starting the server, so a missing store is an actionable exit(2).
    effective_store_dir = _resolve_cli_store_dir(store_dir).path
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter("uvicorn is required to run the proxy server: pip install uvicorn") from exc
    console.print("Starting cost proxy. Forwarding is enabled only for explicitly allowlisted providers.")
    uvicorn.run(
        create_app(
            store_dir=effective_store_dir,
            policy=CostPolicy(
                max_total_usd=max_total_usd,
                max_total_tokens=max_total_tokens,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
            ),
            dry_run=not enable_forwarding,
            enable_forwarding=enable_forwarding,
            allowed_forward_providers=allowed_forward_providers,
            openrouter_api_key=openrouter_api_key,
            openai_api_key=openai_api_key,
            deepseek_api_key=deepseek_api_key,
            extra_allowed_hosts=tuple(allow_host or ()),
        ),
        host=host,
        port=port,
    )


def _receipt_cost_text(cost: dict[str, Any]) -> str:
    amount = cost.get("estimated_cost_usd")
    if amount is None:
        return "—"
    basis = cost.get("cost_basis") or "unknown basis"
    suffix = "" if cost.get("cost_complete") else " (partial)"
    return f"${float(amount):.2f} · {basis}{suffix}"


def _receipt_category_text(counts: dict[str, Any]) -> str:
    if not counts:
        return "not instrumented"
    return " ".join(f"{name}×{value}" for name, value in sorted(counts.items()))


def _find_receipt_task(projection: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    tasks = [
        task
        for task in projection.get("tasks", [])
        if isinstance(task, dict) and str(task.get("public_task_id") or "")
    ]
    exact = next((task for task in tasks if str(task.get("public_task_id")) == task_id), None)
    if exact is not None:
        return exact
    # Convenience: an unambiguous id prefix (e.g. the first 12 chars) resolves.
    matches = [task for task in tasks if str(task.get("public_task_id")).startswith(task_id)]
    return matches[0] if len(matches) == 1 else None


def _render_receipt_text(receipt: dict[str, Any]) -> None:
    from .receipt import evidence_coverage_headline, evidence_coverage_ledger

    axes = receipt.get("axes", {})
    dims = receipt.get("dimensions", {})
    decision = axes.get("decision_status", {})
    evidence = axes.get("evidence_strength", {})

    console.print(f"[bold]Work Receipt[/bold] · {_rich_escape(str(receipt.get('title') or 'Task'))}")
    console.print(f"[dim]{_rich_escape(str(receipt.get('task_id') or ''))}[/dim]\n")

    console.print(
        f"  Decision status    [bold]{str(decision.get('key') or 'unknown').upper()}[/bold]"
        f"  (asserted by {decision.get('asserted_by') or 'none'})"
    )
    if decision.get("statement"):
        console.print(f"                     [dim]{_rich_escape(str(decision['statement']))}[/dim]")
    handoff = axes.get("handoff", {})
    if handoff.get("handed_off") and str(decision.get("key") or "") != "handed_off":
        # The deliberate-stop lifecycle marker, shown BESIDE the decision word (not
        # instead of it) so a finding/blocked headline never hides the handoff.
        console.print("  Lifecycle          [magenta]↗ Handed off[/magenta]")
        if handoff.get("statement"):
            console.print(f"                     [dim]{_rich_escape(str(handoff['statement']))}[/dim]")
    console.print(f"  Evidence coverage  [bold]{_rich_escape(evidence_coverage_headline(evidence))}[/bold]")
    ledger = evidence_coverage_ledger(evidence)
    if ledger:
        console.print(f"                     [dim]{_rich_escape(ledger)}[/dim]")
    if evidence.get("definition"):
        console.print(f"                     [dim]{_rich_escape(str(evidence['definition']))}[/dim]")
    if axes.get("orthogonality_note"):
        console.print(f"  [dim]{axes['orthogonality_note']}[/dim]")

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Dimension")
    table.add_column("Summary")
    table.add_column("Source", style="dim")

    task = dims.get("task", {})
    objectives = task.get("objectives") or []
    boundary = task.get("boundary", {})
    task_summary = "; ".join(objectives[:2]) or "no objective recorded"
    if boundary.get("project"):
        task_summary += f"  · project {boundary['project']}"
    table.add_row("Task", _rich_escape(task_summary), ", ".join(task.get("provenance") or []))

    actors = dims.get("actors", {})
    actor_summary = " · ".join(
        part
        for part in (
            actors.get("primary_agent"),
            ", ".join(actors.get("models") or []) or None,
            (f"{actors.get('subagent_session_count')} subagents" if actors.get("subagent_session_count") else None),
        )
        if part
    )
    table.add_row("Actors", _rich_escape(actor_summary or "—"), ", ".join(actors.get("provenance") or []))

    actions = dims.get("actions", {})
    actions_summary = _receipt_category_text(actions.get("tool_category_counts") or {})
    actions_summary += f"  · touched {int(actions.get('touched_file_count') or 0)} file(s)"
    shown_files = actions.get("touched_files_preview") or []
    elided_files = int(actions.get("touched_files_elided") or 0)
    if shown_files:
        # Show the actual artifact paths, not just the count. The daemon already
        # capped the slice and disclosed the overflow; only the count was ever
        # surfaced before.
        lines = "\n".join(str(path) for path in shown_files)
        if elided_files:
            lines += f"\n… +{elided_files} more"
        actions_summary += f"\n[dim]{_rich_escape(lines)}[/dim]"
    table.add_row("Actions", actions_summary, ", ".join(actions.get("provenance") or []))

    cost = dims.get("cost", {})
    table.add_row("Cost", _receipt_cost_text(cost), ", ".join(cost.get("provenance") or []))

    evidence_dim = dims.get("evidence", {})
    table.add_row(
        "Evidence",
        f"{int(evidence_dim.get('checks_total') or 0)} checks · "
        f"{int(evidence_dim.get('checks_passed') or 0)} passed · "
        f"{int(evidence_dim.get('checks_failed') or 0)} failed",
        ", ".join(evidence_dim.get("provenance") or []),
    )

    outcome = dims.get("outcome", {})
    table.add_row(
        "Outcome",
        f"{outcome.get('decision_status')} · asserted by {outcome.get('asserted_by')}",
        ", ".join(outcome.get("provenance") or []),
    )
    console.print(table)

    gaps = dims.get("gaps", {})
    if gaps.get("items"):
        console.print(f"\n[bold]Gaps[/bold] ({gaps.get('count')}) — what could not be proven")
        for item in gaps["items"]:
            console.print(f"  · \\[{item.get('dimension')}] {_rich_escape(str(item.get('reason')))}")

    legend = dims.get("provenance", {}).get("legend") or {}
    if legend:
        console.print("\n[bold]Provenance[/bold]")
        for source, description in legend.items():
            console.print(f"  {source} — [dim]{description}[/dim]")


@app.command("receipts")
def receipts(
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    limit: Annotated[int, typer.Option(help="Maximum number of Tasks to list.")] = 20,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the raw summary JSON.")] = False,
) -> None:
    """List recent Tasks with their Receipt summary (decision × evidence, cost)."""

    from .api import build_store_task_projection, _task_title
    from .receipt import (
        RECEIPT_SCHEMA_VERSION,
        build_receipt_summary,
        evidence_coverage_headline,
        latest_store_activity,
    )

    resolved = _resolve_cli_store_dir(store_dir).path
    projection = build_store_task_projection(resolved)
    tasks = [
        task
        for task in projection.get("tasks", [])
        if isinstance(task, dict) and str(task.get("public_task_id") or "")
    ]
    latest = latest_store_activity(tasks)
    tasks.sort(key=lambda task: float(task.get("last_activity_at") or 0.0), reverse=True)
    rows = [
        build_receipt_summary(
            task,
            public_task_id=str(task.get("public_task_id")),
            title=_task_title(task),
            latest_store_activity_at=latest,
        )
        for task in tasks[:limit]
    ]
    if json_output:
        console.print_json(data={"schema": RECEIPT_SCHEMA_VERSION, "tasks": rows})
        return
    if not rows:
        console.print("No Tasks recorded in this store yet.")
        return
    table = Table(title="Work Receipts", header_style="bold")
    table.add_column("Task")
    table.add_column("Title")
    table.add_column("Decision")
    table.add_column("Evidence coverage")
    table.add_column("Cost")
    for row in rows:
        decision_key = str(row["decision_status"]["key"])
        decision_cell = decision_key
        if row.get("handed_off") and decision_key != "handed_off":
            # Parallel lifecycle marker: shown beside a finding/blocked/… word so a
            # clean handoff is never hidden by the louder problem.
            decision_cell += "  [magenta]↗ handed off[/magenta]"
        table.add_row(
            str(row["task_id"])[:16],
            _rich_escape(str(row.get("title") or "")[:48]),
            decision_cell,
            _rich_escape(evidence_coverage_headline(row.get("evidence_strength") or {})),
            _receipt_cost_text(row.get("cost") or {}),
        )
    console.print(table)


@app.command("receipt")
def receipt(
    task_id: Annotated[str, typer.Argument(help="Public Task id (task_…); an unambiguous prefix works.")],
    store_dir: Annotated[Optional[Path], typer.Option(help=_STORE_DIR_HELP)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the full agentacct.receipt.v1 JSON.")] = False,
) -> None:
    """Print one Task's full Work Receipt: the 8 questions, the two orthogonal
    axes (decision status × evidence strength), per-field provenance, and gaps."""

    from .api import build_store_task_projection, _task_title
    from .receipt import build_receipt, latest_store_activity

    resolved = _resolve_cli_store_dir(store_dir).path
    projection = build_store_task_projection(resolved)
    task = _find_receipt_task(projection, task_id)
    if task is None:
        console.print(f"No Task matches {task_id} in this store.")
        raise typer.Exit(1)
    all_tasks = [t for t in projection.get("tasks", []) if isinstance(t, dict)]
    receipt_payload = build_receipt(
        task,
        public_task_id=str(task.get("public_task_id")),
        title=_task_title(task),
        latest_store_activity_at=latest_store_activity(all_tasks),
    )
    if json_output:
        console.print_json(data=receipt_payload)
        return
    _render_receipt_text(receipt_payload)


if __name__ == "__main__":
    app()
