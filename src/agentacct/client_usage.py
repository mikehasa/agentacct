from __future__ import annotations

import contextvars
import copy
import errno
import hashlib
import json
import math
import os
import pickle
import re
import sqlite3
import stat
import time
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Literal, TextIO

from .codex_rollout_adapter import (
    CodexEvidenceFragment,
    codex_function_action_name,
    codex_mcp_action_name,
    codex_model_from_record,
    codex_record_may_contain_evidence,
    decode_codex_evidence_fragment,
    reconcile_codex_evidence_fragments,
    validated_codex_identifier,
)
from .confidence import COST_BASIS_CLIENT_SESSION, COST_BASIS_PRICING_TABLE, COST_CLIENT_REPORTED, COST_ESTIMATED_FROM_TOKENS, COST_UNKNOWN, USAGE_CLIENT_REPORTED
from .env_compat import read_env_alias
from .cost import estimate_model_cost_breakdown_usd, has_model_price, model_pricing_entry
from .log_evidence import (
    ACCEPTED_SERVER_KEYS,
    LogEvidenceAccumulator,
    classify_claude_tool_use,
    claude_tool_use_creation_tool,
    opencode_tool_creation_tool,
    refused_recording_rows,
)
from .source_paths import resolve_core_usage_source_plans, resolve_usage_source_paths
from .store_resolution import claude_worktree_owner_path_text
from .usage_truth import (
    CODEX_LINEAGE_DELTA_SEMANTICS,
    LOCAL_USAGE_SOURCE,
    LOCAL_SESSION_OBSERVATION_SOURCE,
    USAGE_KEY_MIGRATION_REASON,
    USAGE_ROW_LANE_PREFIX,
    is_legacy_local_usage_import_shape,
    is_local_usage_import_event,
    local_usage_additivity,
    local_usage_event_key,
    local_usage_row_identity,
    normalized_local_usage_session_id,
    recognized_local_usage_row_identity,
    sanitize_session_key_component,
)
from .tool_activity import (
    _COMMANDS_PER_BATCH_MAX,
    _TOUCHED_FILES_PER_BATCH_MAX,
    _normalize_command,
    _normalize_touched_path,
    normalize_tool_name,
    tool_category,
)
from .mechanical_capture import classify_command, command_digest

UsageClientName = Literal["codex", "claude-code", "opencode", "hermes", "openclaw"]
ObservedClientName = Literal[
    "codex", "claude-code", "opencode", "hermes", "openclaw", "cursor"
]
# Local clients agentacct can inspect. Cursor is intentionally observation-only:
# it belongs in discovery/import routing, but never in the usage-event subset.
SUPPORTED_CLIENTS: tuple[str, ...] = (
    "codex",
    "claude-code",
    "opencode",
    "hermes",
    "openclaw",
    "cursor",
)
USAGE_EVENT_CLIENTS: tuple[str, ...] = (
    "codex",
    "claude-code",
    "opencode",
    "hermes",
    "openclaw",
)
_MAX_SESSION_TITLE_LENGTH = 240
_CLAUDE_IDENTITY_SCAN_MAX_BYTES = 256 * 1024
_CLAUDE_IDENTITY_SCAN_MAX_LINES = 256
_CLAUDE_WORKFLOW_JOURNAL_MAX_BYTES = 8 * 1024 * 1024
_CLAUDE_WORKFLOW_JOURNAL_MAX_LINES = 8_192


class _ClientUsageDiscoveryReadError(RuntimeError):
    """Expected local-source read failure with a path-free diagnostic code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ClaudeTranscriptUnsafePathError(OSError):
    """A discovered Claude transcript path escaped the configured source root."""


class _ClaudeTranscriptChangedDuringScanError(OSError):
    """A Claude transcript was replaced between identity and usage reads."""


_ClaudeFileFingerprint = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _RegularSourceFile:
    """One regular file proven to live beneath its configured source root."""

    path: Path
    root: Path
    mtime: float
    mtime_ns: int
    device: int
    inode: int
    size: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem


@dataclass(frozen=True)
class _NoFollowTreeFile:
    """One file observed during an exhaustive descriptor-relative tree walk."""

    path: Path
    fingerprint: tuple[int, int, int, int, int] | None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem


@dataclass(frozen=True)
class _HermesStateDbPlan:
    """Regular Hermes databases plus the trust result for configured homes."""

    sources: tuple[_RegularSourceFile, ...]
    configured_home_count: int
    unresolved_home_count: int
    explicit_home: bool

    @property
    def requires_explicit_selection(self) -> bool:
        """Multiple env homes are safe only when every alias is one inode."""

        return (
            not self.explicit_home
            and self.configured_home_count > 1
            and (
                self.unresolved_home_count > 0
                or len(self.sources) != 1
            )
        )


@dataclass(frozen=True)
class _HermesStateDbScan:
    """Bounded Hermes rows with the pre-limit source cardinality retained."""

    discovered_rows: int
    selected_rows: tuple[dict[str, Any], ...]


class _CursorStateDbReadError(_ClientUsageDiscoveryReadError):
    """Stable Cursor source error that may retain pre-failure row counts."""

    def __init__(self, code: str, *, discovered_rows: int = 0) -> None:
        super().__init__(code)
        self.discovered_rows = max(0, discovered_rows)


@dataclass(frozen=True)
class ClientUsageDiscoveryResult:
    """Usage candidates plus source-level, JSON-ready scan diagnostics."""

    events: list[ClientUsageEvent]
    diagnostics: dict[str, dict[str, Any]]
    session_observations: list[ClientSessionObservation] = field(default_factory=list)

_DEFAULT_CLIENT_HOME_LABELS: dict[str, str] = {
    "codex": "~/.codex",
    "claude-code": "~/.claude",
    "opencode": "~/.local/share/opencode",
    "hermes": "~/.hermes",
    "openclaw": "~/.openclaw (and related roots)",
    "cursor": "~/Library/Application Support/Cursor",
}


def describe_scanned_client_homes(
    *,
    client: str = "all",
    codex_home: "Path | None" = None,
    claude_home: "Path | None" = None,
    opencode_home: "Path | None" = None,
    hermes_home: "Path | None" = None,
    openclaw_home: "Path | None" = None,
    cursor_home: "Path | None" = None,
) -> list[str]:
    """Human ``client: home`` labels for exactly the clients a scan inspects.

    Reflects both the ``--client`` filter and any ``--*-home`` override so a
    zero-result import hint names what was ACTUALLY scanned, never the five
    default homes regardless of the flags (the watcher-truth rule forbids
    claiming scans the run did not make).
    """

    overrides = {
        "codex": codex_home,
        "claude-code": claude_home,
        "opencode": opencode_home,
        "hermes": hermes_home,
        "openclaw": openclaw_home,
        "cursor": cursor_home,
    }
    core_plans = resolve_core_usage_source_plans(codex_home=codex_home, claude_home=claude_home)
    labels: list[str] = []
    for name in SUPPORTED_CLIENTS:
        if client not in {"all", name}:
            continue
        if name in core_plans:
            labels.extend(f"{name}: {path}" for path in core_plans[name].homes)
            continue
        override = overrides[name]
        if name == "hermes" and override is None:
            configured = [
                value.strip()
                for value in (_env_text("HERMES_HOME") or "").split(",")
                if value.strip()
            ]
            if configured:
                labels.extend(
                    f"hermes: {Path(value).expanduser()}"
                    for value in configured
                )
                continue
        if name == "cursor":
            cursor_root = (
                Path(override).expanduser()
                if override is not None
                else Path.home() / "Library" / "Application Support" / "Cursor"
            )
            labels.append(
                f"cursor: {cursor_root / 'User' / 'globalStorage' / 'state.vscdb'}"
            )
            continue
        home = str(override) if override is not None else _DEFAULT_CLIENT_HOME_LABELS[name]
        labels.append(f"{name}: {home}")
    return labels


@dataclass(frozen=True)
class ClientUsageEvent:
    """Sanitized local usage summary imported from a coding-agent client store."""

    client: UsageClientName
    client_session_id: str
    source_path: Path
    title: str | None
    cwd: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_tokens_reported: bool | None = None
    cache_read_tokens_reported: bool | None = None
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    provider_name: str | None = None
    started_at: int | None = None
    updated_at: int | None = None
    turn_count: int = 0
    client_reported_cost_usd: float | None = None
    client_cost_source: str | None = None
    client_session_kind: str = "root"
    parent_client_session_id: str | None = None
    client_transcript_id: str | None = None
    client_spawn_status: str | None = None
    client_thread_source: str | None = None
    client_model_source: str | None = None
    client_model_inherited_from_session_id: str | None = None
    raw_usage_rows: int = 0
    deduplicated_usage_rows: int = 0
    usage_row_lane: str | None = None
    usage_update_semantics_override: str | None = None
    raw_cumulative_input_tokens: int | None = None
    raw_cumulative_cached_input_tokens: int | None = None
    raw_cumulative_output_tokens: int | None = None
    raw_cumulative_reasoning_output_tokens: int | None = None
    replay_baseline_input_tokens: int | None = None
    replay_baseline_cached_input_tokens: int | None = None
    replay_baseline_output_tokens: int | None = None
    replay_baseline_reasoning_output_tokens: int | None = None
    replay_prefix_token_events: int = 0
    # Client-log evidence (log_evidence.py): event ids of agentacct-recorded
    # events whose creation responses appear in THIS session's own client log,
    # extracted from the five creation tools' outputs only. The total keeps
    # over-cap counts honest; skipped counts failed/malformed outputs so wire
    # drift surfaces instead of silently yielding zero links.
    evidenced_event_ids: tuple[str, ...] = ()
    evidenced_event_id_total: int = 0
    evidenced_outputs_skipped: int = 0
    source_namespace_fingerprint: str | None = None
    source_parse_complete: bool = True
    # Appended to retain the positional constructor contract. Numeric
    # compatibility values remain available to existing product surfaces,
    # while these flags record whether the source actually carried each
    # counter. Codex's sqlite ``tokens_used`` fallback therefore cannot turn
    # an absent input/output dimension into a measured zero.
    input_tokens_reported: bool | None = None
    output_tokens_reported: bool | None = None
    reasoning_output_tokens_reported: bool | None = None
    total_tokens: int | None = None
    total_tokens_reported: bool | None = None
    usage_representation: str | None = None
    usage_precedence_role: Literal["authoritative", "fallback", "enrichment"] | None = None
    # Import-source revision watermark in MICROSECONDS, mirroring the
    # observation lane: whole-second client clocks cannot order two real
    # revisions inside one displayed second, which the refreshable lane would
    # otherwise park as a permanent equal-order conflict. Only per-session
    # carriers may populate this; a shared container clock (hermes/codex
    # sqlite file mtime) must stay out because it advances for unrelated
    # sessions. Ordering provenance only — excluded from stored-row compare
    # and from refreshable truth material.
    source_revision_at: int | None = None
    source_revision_basis: str | None = None
    # Read-time diagnostic (log_evidence.py): the subset of
    # ``evidenced_outputs_skipped`` this scan can prove was a recording call
    # agentacct itself REFUSED, as bounded {tool, field, reason_code, count}
    # rows. Derived fresh from the client log on every scan and deliberately
    # never persisted — the transcripts on disk stay the record, so refusals
    # that predate this field are counted without any backfill. Carries no
    # message text, value, length, or path.
    refused_recording_attempts: tuple[Mapping[str, Any], ...] = ()
    # Token-additivity inputs, kept distinct from the Task-grouping fields
    # (``client_session_kind`` / ``parent_client_session_id``). Codex splits a
    # fork/resume/compaction into its own root Task, but its raw counter may
    # still carry a replayed parent prefix, so the additivity guard must judge
    # it against the EXACT recorded lineage, not the grouping view. When unset
    # (every non-Codex client), additivity falls back to the grouping fields —
    # byte-identical to the pre-split behavior.
    token_lineage_session_kind: str | None = None
    token_lineage_parent_client_session_id: str | None = None
    # Discovery-side Actions signals derived from the client's own transcript/DB
    # when its hook does not capture them — currently OpenCode, from the ``part``
    # table's tool calls (Codex rides the analogous carrier on its
    # ClientSessionObservation). ``rollout_tool_activity`` holds the same
    # {tool_category_counts / tool_names / touched_files / commands} shape the hook
    # drain emits; ``rollout_mechanical_checks`` holds spool-shaped check ticks (a
    # bash exit code the harness observed, which lifts a step to
    # ``independently_checked``). INTERNAL carriers: the import orchestrator emits
    # them as separate tool_activity_observed / machine_check_observed events;
    # ``to_sentinel_event`` never reads them, and ``compare=False`` keeps them out
    # of the frozen dataclass's identity/hash and every stored-row equality (a usage
    # row differing only in what tools it ran is the same row).
    rollout_tool_activity: Mapping[str, Any] | None = field(default=None, compare=False)
    rollout_mechanical_checks: tuple[Mapping[str, Any], ...] = field(
        default=(), compare=False
    )

    def __post_init__(self) -> None:
        if self.client not in USAGE_EVENT_CLIENTS:
            raise ValueError(
                f"{self.client!r} is observation-only and cannot emit usage"
            )
        for field_name in (
            "input_tokens_reported",
            "output_tokens_reported",
            "reasoning_output_tokens_reported",
            "total_tokens_reported",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{field_name} must be boolean or None")
        if self.usage_precedence_role not in {
            None,
            "authoritative",
            "fallback",
            "enrichment",
        }:
            raise ValueError("usage_precedence_role is invalid")

    @property
    def usage_row_identity(self) -> tuple[str, str, str]:
        """Stable row identity: (client, session id, per-model lane or '')."""

        return (self.client, self.client_session_id, self.usage_row_lane or "")

    @property
    def source_session_identity(self) -> tuple[str, str, str]:
        """Client-home-scoped session identity for multi-home discovery."""

        return (
            self.source_namespace_fingerprint or "",
            self.client,
            self.client_session_id,
        )

    @property
    def provider(self) -> str:
        if self.provider_name:
            return self.provider_name
        if self.client == "codex":
            return "codex"
        if self.client == "opencode":
            return "opencode"
        if self.client == "hermes":
            return "hermes"
        if self.client == "openclaw":
            return "openclaw"
        return "claude-code"

    @property
    def source(self) -> str:
        return f"{self.client}-local-session-import"

    @property
    def usage_update_semantics(self) -> str:
        if self.usage_update_semantics_override:
            return self.usage_update_semantics_override
        if self.client == "codex":
            return "codex_rollout_token_count_events"
        if self.client == "opencode":
            return "opencode_step_finish_events"
        if self.client == "hermes":
            return "hermes_state_db_session_rows"
        if self.client == "openclaw":
            return "openclaw_assistant_usage_rows"
        return "claude_assistant_message_usage_rows"

    @property
    def sentinel_run_id(self) -> str:
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in self.client_session_id)
        return f"client_{self.client.replace('-', '_')}_{safe[:80]}"

    def to_sentinel_event(self) -> dict[str, Any]:
        cost_confidence = COST_CLIENT_REPORTED if self.client_reported_cost_usd is not None else COST_UNKNOWN
        session_title = _sanitized_session_title(self.title)
        # Additivity judges the TOKEN lineage, not the Task grouping: a Codex
        # fork/resume split into its own root Task still carries a replayed
        # parent prefix in its raw counter. Prefer the exact-lineage fields;
        # fall back to the grouping fields for clients that never set them.
        additivity_session_kind = (
            self.token_lineage_session_kind
            if self.token_lineage_session_kind is not None
            else self.client_session_kind
        )
        additivity_parent_session_id = (
            self.token_lineage_parent_client_session_id
            if self.token_lineage_parent_client_session_id is not None
            else self.parent_client_session_id
        )
        usage_additive, usage_normalization_state = local_usage_additivity(
            client=self.client,
            session_kind=additivity_session_kind,
            parent_client_session_id=additivity_parent_session_id,
            usage_update_semantics=self.usage_update_semantics,
            source_namespace_fingerprint=self.source_namespace_fingerprint,
            parent_source_namespace_fingerprint=(
                self.source_namespace_fingerprint
                if additivity_parent_session_id
                else None
            ),
        )
        event: dict[str, Any] = {
            "source": self.source,
            "event_type": "model_usage",
            "run_id": self.sentinel_run_id,
            "provider": self.provider,
            "model": self.model,
            "estimated_input_tokens": self.input_tokens,
            "estimated_output_tokens": self.output_tokens,
            "estimated_cost_usd": self.client_reported_cost_usd,
            "usage_confidence": USAGE_CLIENT_REPORTED,
            "cost_confidence": cost_confidence,
            "cost_basis": COST_BASIS_CLIENT_SESSION,
            "metadata": {
                "usage_source": LOCAL_USAGE_SOURCE,
                "usage_update_semantics": self.usage_update_semantics,
                "usage_additive": usage_additive,
                "usage_normalization_state": usage_normalization_state,
                "client": self.client,
                "client_session_id": self.client_session_id,
                "client_session_kind": self.client_session_kind,
                "parent_client_session_id": self.parent_client_session_id,
                "client_transcript_id": self.client_transcript_id,
                "client_spawn_status": self.client_spawn_status,
                "client_thread_source": self.client_thread_source,
                "source_file": self.source_path.name,
                "project_dir": self.cwd,
                # The title itself is safe to persist only because importers
                # source it from an explicit client title field and sanitize it
                # below; prompts/transcript message content remain out of scope.
                # Keep the legacy marker semantically honest: true means a
                # source title existed but was withheld after sanitization;
                # false means it was persisted or there was no title to redact.
                "title_redacted": self.title is not None and session_title is None,
                "cached_input_tokens": self.cached_input_tokens,
                "cache_creation_input_tokens": self.cache_creation_input_tokens,
                "cache_read_input_tokens": self.cache_read_input_tokens,
                "cache_creation_tokens_reported": self.cache_creation_tokens_reported,
                "cache_read_tokens_reported": self.cache_read_tokens_reported,
                "cache_creation_5m_input_tokens": self.cache_creation_5m_input_tokens,
                "cache_creation_1h_input_tokens": self.cache_creation_1h_input_tokens,
                "reasoning_output_tokens": self.reasoning_output_tokens,
                "client_reported_cost_usd": self.client_reported_cost_usd,
                "client_cost_source": self.client_cost_source,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "turn_count": self.turn_count,
                "raw_usage_rows": self.raw_usage_rows,
                "deduplicated_usage_rows": self.deduplicated_usage_rows,
            },
        }
        if self.source_revision_at is not None:
            event["metadata"]["source_revision_at"] = self.source_revision_at
            event["metadata"]["source_revision_basis"] = self.source_revision_basis
        if self.client == "codex":
            # These are source-presence facts, not estimates.  Keep the legacy
            # numeric fields for compatibility, but let canonical consumers
            # distinguish an absent counter from an explicit measured zero.
            presence = {
                "input_tokens_reported": self.input_tokens_reported,
                "output_tokens_reported": self.output_tokens_reported,
                "reasoning_output_tokens_reported": (
                    self.reasoning_output_tokens_reported
                ),
                "total_tokens_reported": self.total_tokens_reported,
            }
            event["metadata"].update(
                {key: value for key, value in presence.items() if value is not None}
            )
            if self.total_tokens_reported is not None:
                event["metadata"]["total_tokens"] = self.total_tokens
        if self.usage_representation is not None:
            event["metadata"]["usage_representation"] = self.usage_representation
        if self.usage_precedence_role is not None:
            event["metadata"]["precedence_role"] = self.usage_precedence_role
        if self.raw_cumulative_input_tokens is not None:
            event["metadata"]["raw_cumulative_input_tokens"] = self.raw_cumulative_input_tokens
            event["metadata"]["raw_cumulative_cached_input_tokens"] = (
                self.raw_cumulative_cached_input_tokens or 0
            )
            event["metadata"]["raw_cumulative_output_tokens"] = (
                self.raw_cumulative_output_tokens or 0
            )
            event["metadata"]["raw_cumulative_reasoning_output_tokens"] = (
                self.raw_cumulative_reasoning_output_tokens or 0
            )
        if self.replay_baseline_input_tokens is not None:
            event["metadata"]["replay_baseline_input_tokens"] = self.replay_baseline_input_tokens
            event["metadata"]["replay_baseline_cached_input_tokens"] = (
                self.replay_baseline_cached_input_tokens or 0
            )
            event["metadata"]["replay_baseline_output_tokens"] = (
                self.replay_baseline_output_tokens or 0
            )
            event["metadata"]["replay_baseline_reasoning_output_tokens"] = (
                self.replay_baseline_reasoning_output_tokens or 0
            )
            event["metadata"]["replay_prefix_token_events"] = self.replay_prefix_token_events
        if session_title is not None:
            event["metadata"]["client_session_title"] = session_title
            event["metadata"]["client_session_title_source"] = "explicit_client_title_field"
            event["metadata"]["client_session_title_sanitized"] = True
        if self.client_model_source is not None:
            event["metadata"]["client_model_source"] = self.client_model_source
        if self.client_model_inherited_from_session_id is not None:
            event["metadata"]["client_model_inherited_from_session_id"] = self.client_model_inherited_from_session_id
        if self.usage_row_lane is not None:
            event["metadata"]["usage_row_lane"] = self.usage_row_lane
        if self.source_namespace_fingerprint is not None:
            event["metadata"]["source_namespace_fingerprint"] = self.source_namespace_fingerprint
            if self.parent_client_session_id is not None:
                event["metadata"]["parent_source_namespace_fingerprint"] = (
                    self.source_namespace_fingerprint
                )
        # Client-log evidence rides the row only when present, so rows from
        # other clients / legacy paths stay byte-identical. These metadata
        # keys are read back ONLY through the usage-truth trust gate
        # (log_evidence.build_log_evidence_index); MCP writers get the trust
        # markers stripped, so they can never mint a donor row.
        if self.evidenced_event_id_total:
            event["metadata"]["evidenced_event_ids"] = list(self.evidenced_event_ids)
            event["metadata"]["evidenced_event_id_total"] = self.evidenced_event_id_total
        if self.evidenced_outputs_skipped:
            event["metadata"]["evidenced_outputs_skipped"] = self.evidenced_outputs_skipped
        # Import-time complement to the read-time worktree label remap: new
        # rows self-describe the owning repo of a `.claude/worktrees/<name>`
        # cwd (pure string parse, no filesystem access). Additive only —
        # `project_dir` stays the verbatim cwd, and read-time remapping still
        # covers historical rows that lack this field.
        if self.cwd:
            owner_dir = claude_worktree_owner_path_text(self.cwd)
            if owner_dir is not None:
                event["metadata"]["project_owner_dir"] = owner_dir
        return event


@dataclass(frozen=True)
class ClientSessionObservation:
    """A real local client session whose usage may still be unavailable.

    This is deliberately not a ``ClientUsageEvent``.  Its persisted event has
    no token, cost, provider, or usage-confidence fields, so a missing usage
    record can never turn into a measured zero merely to keep the session
    visible.
    """

    client: ObservedClientName
    client_session_id: str
    source_path: Path
    title: str | None = None
    cwd: str | None = None
    observed_models: tuple[str, ...] = ()
    started_at: int | None = None
    updated_at: int | None = None
    # Import-source revision ordering is separate from the user-facing
    # activity clock. Filesystems expose nanosecond mtimes even when the
    # client metadata only records whole seconds, so two real revisions in
    # the same displayed second can still be ordered without a false
    # same-watermark conflict.
    source_revision_at: int | float | None = None
    source_revision_basis: str | None = None
    client_session_kind: str = "root"
    parent_client_session_id: str | None = None
    client_transcript_id: str | None = None
    client_spawn_status: str | None = None
    client_thread_source: str | None = None
    observation_basis: str = "local_client_session_record"
    activity_time_basis: str = "client_metadata"
    evidenced_event_ids: tuple[str, ...] = ()
    evidenced_event_id_total: int = 0
    evidenced_outputs_skipped: int = 0
    source_namespace_fingerprint: str | None = None
    source_parse_complete: bool = True
    # Same read-time refusal diagnostic as ClientUsageEvent; never persisted.
    refused_recording_attempts: tuple[Mapping[str, Any], ...] = ()
    # Discovery-side Actions signals (tool_category_counts / tool_names /
    # touched_files / commands) derived from the client's own transcript when its
    # hook does not capture them — currently Codex, from the rollout's tool calls.
    # An INTERNAL carrier: it rides to the import orchestrator, which emits it as a
    # separate ``tool_activity_observed`` event; it is NOT part of the session
    # observation's own sentinel event (to_sentinel_event never reads it).
    rollout_tool_activity: Mapping[str, Any] | None = None

    @property
    def session_identity(self) -> tuple[str, str]:
        return (self.client, self.client_session_id)

    @property
    def source_session_identity(self) -> tuple[str, str, str]:
        """Client-home-scoped identity; raw ids can collide across homes."""

        return (
            self.source_namespace_fingerprint or "",
            self.client,
            self.client_session_id,
        )

    @property
    def source(self) -> str:
        return f"{self.client}-local-session-observation-import"

    @property
    def sentinel_run_id(self) -> str:
        safe = "".join(
            ch if ch.isalnum() or ch in {"_", "-"} else "_"
            for ch in self.client_session_id
        )
        source_suffix = (
            hashlib.sha256(
                self.source_namespace_fingerprint.encode("utf-8")
            ).hexdigest()[:12]
            if self.source_namespace_fingerprint
            else "unscoped"
        )
        return (
            f"client_{self.client.replace('-', '_')}_{safe[:64]}_"
            f"{source_suffix}"
        )

    def to_sentinel_event(self) -> dict[str, Any]:
        session_title = _sanitized_session_title(self.title)
        models = [
            model
            for value in self.observed_models
            if (model := _limited_optional_text(value, 120)) is not None
        ]
        metadata: dict[str, Any] = {
            "observation_source": LOCAL_SESSION_OBSERVATION_SOURCE,
            "client": self.client,
            "client_session_id": self.client_session_id,
            "client_session_kind": self.client_session_kind,
            "parent_client_session_id": self.parent_client_session_id,
            "client_transcript_id": self.client_transcript_id,
            "client_spawn_status": self.client_spawn_status,
            "client_thread_source": self.client_thread_source,
            "source_file": self.source_path.name,
            "source_namespace_fingerprint": self.source_namespace_fingerprint,
            "parent_source_namespace_fingerprint": (
                self.source_namespace_fingerprint
                if self.parent_client_session_id is not None
                else None
            ),
            "project_dir": self.cwd,
            "title_redacted": self.title is not None and session_title is None,
            "observed_models": models,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "source_updated_at": self.updated_at or self.started_at,
            "source_updated_at_basis": self.activity_time_basis,
            "source_revision_at": (
                self.source_revision_at or self.updated_at or self.started_at
            ),
            "source_revision_basis": (
                self.source_revision_basis or self.activity_time_basis
            ),
            "observation_basis": self.observation_basis,
            "activity_time_basis": self.activity_time_basis,
            "source_parse_complete": self.source_parse_complete,
        }
        if session_title is not None:
            metadata["client_session_title"] = session_title
            metadata["client_session_title_source"] = "explicit_client_title_field"
            metadata["client_session_title_sanitized"] = True
        if self.evidenced_event_id_total:
            metadata["evidenced_event_ids"] = list(self.evidenced_event_ids)
            metadata["evidenced_event_id_total"] = self.evidenced_event_id_total
        if self.evidenced_outputs_skipped:
            metadata["evidenced_outputs_skipped"] = self.evidenced_outputs_skipped
        revision_material = json.dumps(
            {
                key: value
                for key, value in metadata.items()
                if key != "idempotency_key"
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        metadata["observation_revision"] = hashlib.sha256(
            revision_material.encode("utf-8")
        ).hexdigest()
        metadata["idempotency_key"] = (
            "local-session-observation:"
            + metadata["observation_revision"]
        )
        return {
            "source": self.source,
            "event_type": "session_observed",
            "run_id": self.sentinel_run_id,
            "metadata": metadata,
        }


def usage_less_session_observations(
    discovery: ClientUsageDiscoveryResult,
) -> list[ClientSessionObservation]:
    """Return trusted presence facts that have no measured usage in this scan.

    A measured row and an observation may be discovered together so root/child
    selection has one identity graph.  Only the observation-only identities
    need their own persisted lane; later measured usage enriches the same
    session without deleting historical presence evidence.
    """

    usage_session_keys = {
        event.source_session_identity
        for event in discovery.events
        if event.source_parse_complete
    }
    latest_by_session: dict[
        tuple[str, str, str], ClientSessionObservation
    ] = {}
    for observation in discovery.session_observations:
        if (
            not observation.source_parse_complete
            or not observation.source_namespace_fingerprint
            or _session_observation_activity(observation) <= 0
            or observation.source_session_identity in usage_session_keys
        ):
            continue
        previous = latest_by_session.get(observation.source_session_identity)
        if previous is None or _session_observation_activity(
            observation
        ) > _session_observation_activity(previous):
            latest_by_session[observation.source_session_identity] = observation
    return sorted(
        latest_by_session.values(),
        key=lambda observation: (
            observation.client,
            observation.client_session_id,
        ),
    )


def complete_session_observation_reconciliation_clients(
    discovery: ClientUsageDiscoveryResult,
) -> set[str]:
    """Clients whose current scan is complete enough to repair quarantine."""

    clients = {
        observation.client for observation in discovery.session_observations
    }
    complete: set[str] = set()
    for client in clients:
        diagnostic = discovery.diagnostics.get(client)
        if not isinstance(diagnostic, dict):
            continue
        if any(
            _safe_nonnegative_int(diagnostic.get(key)) > 0
            for key in (
                "error_count",
                "excluded_by_limit",
                "excluded_by_source_namespace",
                "unparsed_selected_rows",
                "unresolved_identity_files",
            )
        ):
            continue
        if any(
            observation.client == client
            and (
                not observation.source_parse_complete
                or not observation.source_namespace_fingerprint
            )
            for observation in discovery.session_observations
        ):
            continue
        complete.add(client)
    return complete


def discover_codex_usage(
    *,
    codex_home: Path | None = None,
    limit_sessions: int = 20,
    _discovery_stats: dict[str, Any] | None = None,
    _session_observations: list[ClientSessionObservation] | None = None,
) -> list[ClientUsageEvent]:
    """Discover Codex usage from the same source-path plan used by status."""

    source_plan = resolve_usage_source_paths("codex", explicit_home=codex_home)
    homes = source_plan.homes
    per_home_stats: list[dict[str, Any]] = []
    home_batches: list[
        tuple[str, list[ClientUsageEvent], list[ClientSessionObservation]]
    ] = []
    for home in homes:
        scan_home = _canonical_source_home(home)
        stats: dict[str, Any] = {}
        home_observations: list[ClientSessionObservation] = []
        try:
            discovered = _discover_codex_usage_from_home(
                codex_home=scan_home,
                limit_sessions=limit_sessions,
                _discovery_stats=stats,
                _session_observations=home_observations,
            )
        except (_ClientUsageDiscoveryReadError, OSError, RuntimeError, sqlite3.DatabaseError) as exc:
            discovered = []
            _record_per_home_discovery_error(stats, exc)
        namespace = _source_home_namespace(scan_home)
        home_batches.append(
            (
                namespace,
                _with_source_namespace(discovered, namespace),
                _with_observation_source_namespace(
                    home_observations,
                    namespace,
                ),
            )
        )
        per_home_stats.append(stats)
    (
        events,
        observations,
        selected_root_groups,
        namespace_error_codes,
        namespace_error_count,
        global_excluded_by_limit,
        excluded_by_namespace,
    ) = (
        _combine_and_limit_namespaced_session_data(
            home_batches,
            limit_root_groups=limit_sessions,
        )
    )
    if _session_observations is not None:
        _session_observations.extend(observations)
    if source_plan.omitted_home_count:
        namespace_error_codes.append("source_home_plan_truncated")
        namespace_error_count += source_plan.omitted_home_count
    _merge_multi_home_discovery_stats(
        _discovery_stats,
        per_home_stats,
        events=events,
        limit_unit="root_groups",
        selected_root_groups=selected_root_groups,
        extra_error_codes=namespace_error_codes,
        extra_error_count=namespace_error_count,
        extra_excluded_by_limit=global_excluded_by_limit,
        excluded_by_namespace=excluded_by_namespace,
        observations=observations,
    )
    return events


def _discover_codex_usage_from_home(
    *,
    codex_home: Path,
    limit_sessions: int = 20,
    _discovery_stats: dict[str, Any] | None = None,
    _session_observations: list[ClientSessionObservation] | None = None,
) -> list[ClientUsageEvent]:
    """Read Codex's local state/session files and return sanitized usage summaries.

    Codex 0.139 persists thread rows in ~/.codex/state_5.sqlite and per-turn
    token usage in ~/.codex/sessions/**/rollout-*.jsonl. The JSONL's
    total_token_usage.input_tokens is the inclusive prompt-input total. Cache
    reads (cached_input_tokens) and, when the rollout supplies it, cache writes
    (cache_write_tokens) are detail buckets inside that total. agentacct stores
    the remainder in the legacy estimated_input_tokens field and keeps both
    cache buckets in metadata, with row-level reporting flags so absence never
    masquerades as a measured zero.
    """

    root = codex_home.expanduser()
    db_path = root / "state_5.sqlite"
    if _discovery_stats is not None:
        _discovery_stats.update(
            {
                "discovered": 0,
                "parsed": 0,
                "skipped": 0,
                "watermark": None,
                "limit_unit": "root_groups",
                "selected_root_groups": 0,
                "returned_rows": 0,
                "excluded_by_limit": 0,
                "ignored_non_transcript_files": 0,
                "unresolved_identity_files": 0,
                "unparsed_selected_rows": 0,
                "observed_sessions": 0,
                "usage_sessions": 0,
                "sessions_without_usage": 0,
                "error_count": 0,
                "error_codes": [],
                "source_present": False,
            }
        )
    # Lineage must be resolved BEFORE limiting. A flat ``order by updated_at
    # limit N`` lets a burst of child/review threads consume every slot and
    # drops the root they belong to. Read the lightweight sqlite rows first,
    # resolve exact parent ids from Codex's two authoritative carriers, then
    # apply the caller's limit to root groups rather than individual rows.
    db_carrier_declared = False
    try:
        db_path.lstat()
        db_carrier_declared = True
    except FileNotFoundError:
        pass
    except OSError:
        if _discovery_stats is not None:
            _discovery_stats["error_count"] = _safe_nonnegative_int(
                _discovery_stats.get("error_count")
            ) + 1
            _discovery_stats["error_codes"] = [
                *(
                    code
                    for code in _discovery_stats.get("error_codes") or []
                    if isinstance(code, str)
                ),
                "codex_state_db_carrier_unreadable",
            ]
    db_source = _regular_source_file(db_path, root=root)
    if db_carrier_declared and db_source is None and _discovery_stats is not None:
        _discovery_stats["error_count"] = _safe_nonnegative_int(
            _discovery_stats.get("error_count")
        ) + 1
        error_codes = [
            code
            for code in _discovery_stats.get("error_codes") or []
            if isinstance(code, str)
        ]
        if "codex_state_db_carrier_unreadable" not in error_codes:
            error_codes.append("codex_state_db_carrier_unreadable")
        _discovery_stats["error_codes"] = error_codes
    if _discovery_stats is not None:
        # Presence is carried only by a source the importer itself has opened
        # under the configured trust boundary.  A merely non-empty home is not
        # evidence that this is a Codex source.
        _discovery_stats["source_present"] = db_source is not None
    all_rows = (
        _read_codex_thread_rows(db_source, limit_sessions=None)
        if db_source is not None
        else []
    )
    rollout_only_rows = _discover_codex_rollout_only_rows(
        root,
        indexed_rows=all_rows,
        _discovery_stats=_discovery_stats,
    )
    all_rows.extend(rollout_only_rows)
    if _discovery_stats is not None:
        # A rollout-only installation is authoritative when at least one
        # fd-safe regular rollout supplied its own session_meta identity.
        _discovery_stats["source_present"] = bool(
            _discovery_stats["source_present"] or rollout_only_rows
        )
        _discovery_stats["discovered"] = len(all_rows)
    spawn_edges = (
        _read_codex_spawn_edges(db_source)
        if db_source is not None
        else {}
    )
    rollout_source_by_session = {
        row_id: source
        for row in all_rows
        if (row_id := _codex_row_session_id(row))
        and (
            source := _codex_rollout_source(
                Path(str(row.get("rollout_path") or "")),
                codex_root=root,
            )
        )
        is not None
    }
    parent_by_session: dict[str, str | None] = {}
    for row in all_rows:
        row_id = _codex_row_session_id(row)
        spawn_edge = spawn_edges.get(row_id, {})
        parent_session_id = _limited_optional_text(spawn_edge.get("parent_thread_id"), 240)
        if parent_session_id is None:
            rollout_parent = _limited_optional_text(
                row.get("_rollout_parent_thread_id"),
                240,
            ) or (
                _read_codex_rollout_parent_thread_id(rollout_source)
                if (rollout_source := rollout_source_by_session.get(row_id))
                is not None
                else None
            )
            if rollout_parent and rollout_parent != row_id:
                parent_session_id = rollout_parent
        parent_by_session[row_id] = parent_session_id
    rows, selected_root_groups = _select_codex_root_groups(
        all_rows,
        parent_by_session=parent_by_session,
        limit_root_groups=limit_sessions,
    )
    if _discovery_stats is not None:
        _discovery_stats["selected_root_groups"] = selected_root_groups
        _discovery_stats["returned_rows"] = len(rows)
        _discovery_stats["excluded_by_limit"] = max(0, len(all_rows) - len(rows))
    # The lineage pass above reads only the first authoritative session_meta.
    # Full JSONL token/evidence parsing is bounded to selected root groups, so
    # watch cost does not grow with every historical rollout merely because
    # lineage must be resolved before limiting.
    usage_by_session: dict[str, dict[str, Any] | None] = {}
    observation_metadata_by_session: dict[str, dict[str, Any]] = {}
    parse_complete_by_session: dict[str, bool] = {}
    rollout_parse_stats: dict[str, int] = {
        "unparseable_rollouts": 0,
        "schema_drift_rollouts": 0,
        "evidence_schema_drift_rollouts": 0,
    }
    _codex_total_rows = len(rows)
    for _codex_index, row in enumerate(rows):
        row_id = _codex_row_session_id(row)
        row_parse_stats: dict[str, int] = {
            "unparseable_rollouts": 0,
            "schema_drift_rollouts": 0,
            "evidence_schema_drift_rollouts": 0,
        }
        observation_metadata: dict[str, Any] = {}
        rollout_source = rollout_source_by_session.get(row_id)
        usage_by_session[row_id] = (
            _read_codex_rollout_usage(
                rollout_source,
                _parse_stats=row_parse_stats,
                _observation_metadata=observation_metadata,
            )
            if rollout_source is not None
            else None
        )
        observation_metadata_by_session[row_id] = observation_metadata
        parse_complete_by_session[row_id] = not any(
            _safe_nonnegative_int(row_parse_stats.get(key))
            for key in ("unparseable_rollouts", "schema_drift_rollouts")
        )
        for key in rollout_parse_stats:
            rollout_parse_stats[key] += _safe_nonnegative_int(
                row_parse_stats.get(key)
            )
        if _codex_index % _PROGRESS_EMIT_STRIDE == 0 or _codex_index + 1 == _codex_total_rows:
            _emit_scan_progress("codex", _codex_index + 1, _codex_total_rows)
    _normalize_codex_rollout_usage_cohorts(
        rows,
        parent_by_session=parent_by_session,
        usage_by_session=usage_by_session,
        parse_complete_by_session=parse_complete_by_session,
    )
    if _discovery_stats is not None and any(rollout_parse_stats.values()):
        _discovery_stats["error_count"] = _safe_nonnegative_int(
            _discovery_stats.get("error_count")
        ) + sum(rollout_parse_stats.values())
        error_codes: list[str] = [
            code
            for code in _discovery_stats.get("error_codes") or []
            if isinstance(code, str)
        ]
        if rollout_parse_stats["unparseable_rollouts"]:
            if "codex_rollout_unparseable" not in error_codes:
                error_codes.append("codex_rollout_unparseable")
        if rollout_parse_stats["schema_drift_rollouts"]:
            if "codex_rollout_schema_drift" not in error_codes:
                error_codes.append("codex_rollout_schema_drift")
        if rollout_parse_stats["evidence_schema_drift_rollouts"]:
            if "codex_rollout_evidence_schema_drift" not in error_codes:
                error_codes.append("codex_rollout_evidence_schema_drift")
        _discovery_stats["error_codes"] = error_codes
    index_source = _regular_source_file(root / "session_index.jsonl", root=root)
    titles_by_session = (
        _read_codex_session_titles(
            index_source,
            session_ids={str(row.get("id") or "") for row in rows if row.get("id")},
        )
        if index_source is not None
        else {}
    )
    events: list[ClientUsageEvent] = []
    direct_model_by_session = {
        _codex_row_session_id(row): _codex_model_label(
            (usage_by_session.get(_codex_row_session_id(row)) or {}).get("model") or row.get("model")
        )
        for row in rows
    }
    for row in rows:
        row_id = _codex_row_session_id(row)
        spawn_edge = spawn_edges.get(row_id, {})
        rollout_source = rollout_source_by_session.get(row_id)
        source_path = (
            rollout_source.path
            if rollout_source is not None
            else db_source.path
            if db_source is not None
            else Path(str(row.get("rollout_path") or ""))
        )
        usage = usage_by_session.get(row_id)
        usage_representation: str | None = None
        usage_precedence_role: Literal[
            "authoritative", "fallback", "enrichment"
        ] | None = None
        observation_metadata = observation_metadata_by_session.get(row_id, {})
        # Capabilities belong to this observed row, not to the client name.
        # Current Codex rollouts expose cached-input reads but omit writes;
        # future/newer rollouts can opt in simply by carrying the provider's
        # cache_write_tokens field.  The sqlite fallback below reports neither
        # split, so an absent field must stay distinct from a measured zero.
        cache_read_tokens_reported = isinstance(
            usage, dict
        ) and _codex_counter_reported(usage, "cached_input_tokens")
        cache_creation_tokens_reported = isinstance(
            usage, dict
        ) and _codex_counter_reported(usage, "cache_write_tokens")
        # Parent linkage, exact Codex-recorded ids only (never inferred):
        # thread_spawn_edges first (Codex's canonical linkage table), falling
        # back to the rollout file's own session_meta.parent_thread_id — the
        # id Codex wrote at thread creation. In practice spawn_edges is often
        # empty, which left every internal auto-review thread parentless.
        #
        # This exact-recorded parent still drives token-delta counting and
        # discovery grouping (``parent_by_session``, above). It must NOT double
        # as the Task-grouping edge: Codex writes the same parent_thread_id on
        # fork / resume / compaction rollouts, which are the SAME conversation
        # continued, and chaining those transitively merges unrelated work into
        # one mega-Task. The Task boundary carries a parent ONLY for a proven
        # concurrent spawn (or an auto-review child); every other row becomes
        # its own root Task while keeping its token lineage intact.
        parent_session_id = parent_by_session.get(row_id)
        usage_model = (usage or {}).get("model") or row.get("model")
        is_internal_review = _is_codex_internal_review_model(usage_model)
        # Token-lineage kind: the EXACT recorded parent decides token
        # additivity, because a descendant's raw counter may still carry a
        # replayed parent prefix. It stays keyed on the full lineage parent and
        # is never softened by the Task-grouping split below.
        lineage_session_kind = _codex_apply_session_kind_overrides(
            _codex_session_kind(row, parent_session_id=parent_session_id),
            lineage_parent_session_id=parent_session_id,
            usage=usage,
            is_internal_review=is_internal_review,
        )
        # Task-grouping parent + kind: carry a parent (and read as a child)
        # ONLY for a proven concurrent spawn or an auto-review child. A bare
        # fork/resume/compaction lineage edge is dropped so the row becomes its
        # own clean root Task without disturbing its token lineage above.
        carries_grouping_parent = is_internal_review or _codex_edge_is_real_spawn(
            row,
            spawn_edge=spawn_edge,
            usage=usage,
        )
        grouping_parent_session_id = (
            parent_session_id
            if parent_session_id is not None and carries_grouping_parent
            else None
        )
        session_kind = _codex_apply_session_kind_overrides(
            _codex_session_kind(row, parent_session_id=grouping_parent_session_id),
            lineage_parent_session_id=parent_session_id,
            usage=usage,
            is_internal_review=is_internal_review,
        )
        model = direct_model_by_session.get(row_id)
        model_source: str | None = None
        model_inherited_from_session_id: str | None = None
        # An internal Codex label (for example ``codex-auto-review``) is a
        # workflow kind, not a billable model. It may inherit a model ONLY
        # from its exact recorded parent. Never borrow the first model seen in
        # the wider scan: that can cross projects/tasks and silently reprice
        # unrelated internal work.
        if model is None and is_internal_review and parent_session_id is not None:
            parent_model = direct_model_by_session.get(parent_session_id)
            if parent_model is not None:
                model = parent_model
                model_source = "inherited_exact_parent_session"
                model_inherited_from_session_id = parent_session_id

        evidence_source = usage or observation_metadata
        if _session_observations is not None:
            rollout_revision_at = (
                rollout_source.mtime_ns
                if rollout_source is not None
                else db_source.mtime_ns
                if db_source is not None
                else row.get("_source_revision_at")
                or _optional_int(row.get("updated_at"))
            )
            _session_observations.append(
                ClientSessionObservation(
                    client="codex",
                    client_session_id=row_id,
                    source_path=source_path,
                    title=titles_by_session.get(row_id),
                    cwd=_limited_optional_text(
                        row.get("cwd") or observation_metadata.get("cwd"),
                        240,
                    ),
                    observed_models=(model,) if model is not None else (),
                    started_at=(
                        _optional_int(row.get("created_at"))
                        or _optional_int(observation_metadata.get("first_activity_at"))
                    ),
                    updated_at=(
                        _optional_int(row.get("updated_at"))
                        or _optional_int(observation_metadata.get("last_activity_at"))
                    ),
                    source_revision_at=rollout_revision_at,
                    source_revision_basis=(
                        "file_mtime_ns"
                        if row.get("_source_revision_at")
                        or rollout_revision_at
                        != _optional_int(row.get("updated_at"))
                        else "client_metadata"
                    ),
                    client_session_kind=session_kind,
                    # Task-grouping parent only: a bare fork/resume/compaction
                    # lineage edge is dropped here so it becomes its own root
                    # Task. The full lineage parent stays on the usage event
                    # below (token delta + usage windowing).
                    parent_client_session_id=grouping_parent_session_id,
                    client_transcript_id=row_id or rollout_path.stem,
                    client_spawn_status=_limited_optional_text(
                        spawn_edge.get("status"),
                        80,
                    ),
                    client_thread_source=_limited_optional_text(
                        row.get("thread_source") or row.get("source"),
                        120,
                    ),
                    observation_basis=(
                        "codex_rollout_identity"
                        if row.get("source") == "rollout_only"
                        else "codex_state_db_and_rollout_identity"
                        if rollout_source is not None
                        else "codex_state_db"
                    ),
                    activity_time_basis="client_metadata",
                    evidenced_event_ids=tuple(
                        _safe_evidenced_ids(
                            evidence_source.get("evidenced_event_ids")
                        )
                    ),
                    evidenced_event_id_total=_safe_nonnegative_int(
                        evidence_source.get("evidenced_event_id_total")
                    ),
                    evidenced_outputs_skipped=_safe_nonnegative_int(
                        evidence_source.get("evidenced_outputs_skipped")
                    ),
                    refused_recording_attempts=_safe_refused_recording_attempts(
                        evidence_source.get("refused_recording_attempts")
                    ),
                    source_parse_complete=parse_complete_by_session.get(
                        row_id,
                        False,
                    ),
                    rollout_tool_activity=(
                        observation_metadata.get("tool_activity") or None
                    ),
                )
            )

        has_rollout_usage = isinstance(usage, dict) and _safe_nonnegative_int(
            usage.get("_raw_token_event_count")
        ) > 0
        has_rollout_schema_drift = (
            isinstance(usage, dict)
            and usage.get("_token_usage_schema_drift") is True
        )
        if not has_rollout_usage and not has_rollout_schema_drift:
            # No total_token_usage line in the rollout: fall back to sqlite
            # tokens_used exactly as before, keeping any client-log evidence
            # the rollout DID contain riding along on the same row.
            tokens_used = _safe_nonnegative_int(row.get("tokens_used"))
            if tokens_used <= 0:
                continue
            usage = {
                **(usage or {}),
                "input_tokens": tokens_used,
                "_input_tokens_reported": False,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "_output_tokens_reported": False,
                "reasoning_output_tokens": 0,
                "_reasoning_output_tokens_reported": False,
                "total_tokens": tokens_used,
                "_total_tokens_reported": True,
                "turn_count": 0,
                "model": row.get("model"),
                "usage_update_semantics": "codex_sqlite_tokens_used_fallback",
            }
            usage_representation = "codex-sqlite-tokens-used-fallback-v1"
            usage_precedence_role = "fallback"
        (
            total_input,
            cached,
            cache_creation,
            output_tokens,
            reasoning_output_tokens,
            _total_tokens,
        ) = _codex_counter_vector(usage)
        # Codex/OpenAI input_tokens is the inclusive prompt-input total. Both
        # cached reads and (when supplied) cache writes are detail buckets
        # inside that total, so subtract both before storing agentacct's
        # normalized uncached-input field. Adding the three categories back
        # preserves the original total without double counting.
        non_cached_input = max(0, total_input - cached - cache_creation)
        events.append(
            ClientUsageEvent(
                client="codex",
                client_session_id=row_id,
                source_path=source_path,
                # Codex's session index is the explicit sidebar-title source.
                # The sqlite threads.title column can contain the first prompt,
                # so it is intentionally neither selected nor used as fallback.
                title=titles_by_session.get(row_id),
                cwd=_limited_optional_text(row.get("cwd"), 240),
                model=model,
                input_tokens=non_cached_input,
                output_tokens=output_tokens,
                input_tokens_reported=_codex_normalized_input_reported(usage),
                output_tokens_reported=_codex_counter_reported(
                    usage,
                    "output_tokens",
                ),
                cached_input_tokens=cached + cache_creation,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cached,
                cache_creation_tokens_reported=cache_creation_tokens_reported,
                cache_read_tokens_reported=cache_read_tokens_reported,
                reasoning_output_tokens=reasoning_output_tokens,
                reasoning_output_tokens_reported=_codex_counter_reported(
                    usage,
                    "reasoning_output_tokens",
                ),
                total_tokens=(
                    _safe_nonnegative_int(usage.get("total_tokens"))
                    if _codex_counter_reported(usage, "total_tokens")
                    else None
                ),
                total_tokens_reported=_codex_counter_reported(
                    usage,
                    "total_tokens",
                ),
                started_at=_optional_int(row.get("created_at")),
                updated_at=_optional_int(row.get("updated_at")),
                turn_count=_safe_nonnegative_int(usage.get("turn_count")),
                client_session_kind=session_kind,
                # Task-grouping parent, mirroring the observation above: the
                # session rollup unions this field across usage rows AND
                # observations, so a bare fork/resume/compaction lineage edge
                # must be dropped on BOTH to become its own root Task. Token
                # deltas were already resolved against the full lineage parent
                # (``parent_by_session``) before this event was built, so
                # dropping the downstream field never disturbs the numbers.
                parent_client_session_id=grouping_parent_session_id,
                client_transcript_id=row_id or source_path.stem,
                client_spawn_status=_limited_optional_text(spawn_edge.get("status"), 80),
                client_thread_source=_limited_optional_text(row.get("thread_source") or row.get("source"), 120),
                client_model_source=model_source,
                client_model_inherited_from_session_id=model_inherited_from_session_id,
                raw_usage_rows=_safe_nonnegative_int(usage.get("raw_usage_rows")),
                deduplicated_usage_rows=_safe_nonnegative_int(
                    usage.get("deduplicated_usage_rows")
                ),
                usage_update_semantics_override=_limited_optional_text(
                    usage.get("usage_update_semantics"),
                    120,
                ),
                raw_cumulative_input_tokens=_optional_int(
                    usage.get("raw_cumulative_input_tokens")
                ),
                raw_cumulative_cached_input_tokens=_optional_int(
                    usage.get("raw_cumulative_cached_input_tokens")
                ),
                raw_cumulative_output_tokens=_optional_int(
                    usage.get("raw_cumulative_output_tokens")
                ),
                raw_cumulative_reasoning_output_tokens=_optional_int(
                    usage.get("raw_cumulative_reasoning_output_tokens")
                ),
                replay_baseline_input_tokens=_optional_int(
                    usage.get("replay_baseline_input_tokens")
                ),
                replay_baseline_cached_input_tokens=_optional_int(
                    usage.get("replay_baseline_cached_input_tokens")
                ),
                replay_baseline_output_tokens=_optional_int(
                    usage.get("replay_baseline_output_tokens")
                ),
                replay_baseline_reasoning_output_tokens=_optional_int(
                    usage.get("replay_baseline_reasoning_output_tokens")
                ),
                replay_prefix_token_events=_safe_nonnegative_int(
                    usage.get("replay_prefix_token_events")
                ),
                evidenced_event_ids=tuple(_safe_evidenced_ids(usage.get("evidenced_event_ids"))),
                evidenced_event_id_total=_safe_nonnegative_int(usage.get("evidenced_event_id_total")),
                evidenced_outputs_skipped=_safe_nonnegative_int(usage.get("evidenced_outputs_skipped")),
                refused_recording_attempts=_safe_refused_recording_attempts(
                    usage.get("refused_recording_attempts")
                ),
                source_parse_complete=parse_complete_by_session.get(
                    row_id,
                    False,
                ),
                usage_representation=usage_representation,
                usage_precedence_role=usage_precedence_role,
                token_lineage_session_kind=lineage_session_kind,
                token_lineage_parent_client_session_id=parent_session_id,
            )
        )
    if _discovery_stats is not None:
        observations = [
            observation
            for observation in (_session_observations or [])
            if observation.source_parse_complete
        ]
        observed_keys = {observation.session_identity for observation in observations}
        usage_keys = {(event.client, event.client_session_id) for event in events}
        parsed_session_keys = observed_keys | usage_keys
        _discovery_stats["parsed"] = len(parsed_session_keys)
        unparsed_selected_rows = max(0, len(rows) - len(parsed_session_keys))
        _discovery_stats["skipped"] = unparsed_selected_rows
        _discovery_stats["unparsed_selected_rows"] = unparsed_selected_rows
        _discovery_stats["watermark"] = max(
            (
                value
                for value in [
                    *(event.updated_at for event in events),
                    *(observation.updated_at for observation in observations),
                ]
                if value is not None
            ),
            default=None,
        )
        _discovery_stats["observed_sessions"] = len(observed_keys)
        _discovery_stats["usage_sessions"] = len(usage_keys)
        _discovery_stats["sessions_without_usage"] = len(
            observed_keys - usage_keys
        )
    return events


def _safe_evidenced_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _safe_refused_recording_attempts(value: Any) -> tuple[Mapping[str, Any], ...]:
    """Re-validate refusal rows against log_evidence's frozen vocabularies.

    The scan builds these rows itself, but they pass through a plain dict on
    the way here; re-validating means no path from a client log to a rendered
    surface can carry a tool, field, or reason agentacct does not define.
    """

    return tuple(refused_recording_rows(_refused_recording_row_counts(value)))


def _refused_recording_row_counts(value: Any) -> dict[tuple[Any, Any, Any], int]:
    if not isinstance(value, (list, tuple)):
        return {}
    counts: dict[tuple[Any, Any, Any], int] = {}
    for row in value:
        if not isinstance(row, Mapping):
            continue
        key = (row.get("tool"), row.get("field"), row.get("reason_code"))
        counts[key] = counts.get(key, 0) + _safe_nonnegative_int(row.get("count"))
    return counts


def _initial_claude_discovery_stats() -> dict[str, Any]:
    return {
        "discovered": 0,
        "parsed": 0,
        "skipped": 0,
        "watermark": None,
        "limit_unit": "root_groups",
        "selected_root_groups": 0,
        "returned_rows": 0,
        "excluded_by_limit": 0,
        "ignored_non_transcript_files": 0,
        "unresolved_identity_files": 0,
        "unparsed_selected_rows": 0,
        "observed_sessions": 0,
        "usage_sessions": 0,
        "sessions_without_usage": 0,
        "error_count": 0,
        "error_codes": [],
        "skipped_unsafe_paths": 0,
        "source_present": False,
    }


def discover_claude_code_usage(
    *,
    claude_home: Path | None = None,
    limit_sessions: int = 20,
    _discovery_stats: dict[str, Any] | None = None,
    _session_observations: list[ClientSessionObservation] | None = None,
) -> list[ClientUsageEvent]:
    """Discover Claude usage from the same source-path plan used by status."""

    source_plan = resolve_usage_source_paths("claude-code", explicit_home=claude_home)
    homes = source_plan.homes
    per_home_stats: list[dict[str, Any]] = []
    home_batches: list[
        tuple[str, list[ClientUsageEvent], list[ClientSessionObservation]]
    ] = []
    for home in homes:
        scan_home = _canonical_source_home(home)
        namespace = _source_home_namespace(scan_home, already_canonical=True)
        stats = _initial_claude_discovery_stats()
        home_observations: list[ClientSessionObservation] = []
        projects_root_fd: int | None = None
        try:
            projects_root = scan_home / "projects"
            if os.path.lexists(projects_root):
                projects_root_fd, _root_stat = _open_claude_projects_root_fd(
                    projects_root
                )
            discovered = _discover_claude_code_usage_from_home(
                claude_home=scan_home,
                limit_sessions=limit_sessions,
                _discovery_stats=stats,
                projects_root_fd=projects_root_fd,
                _session_observations=home_observations,
            )
        except (_ClientUsageDiscoveryReadError, OSError, RuntimeError, sqlite3.DatabaseError) as exc:
            discovered = []
            _record_per_home_discovery_error(stats, exc)
        finally:
            if projects_root_fd is not None:
                os.close(projects_root_fd)
        home_batches.append(
            (
                namespace,
                _with_source_namespace(discovered, namespace),
                _with_observation_source_namespace(
                    home_observations,
                    namespace,
                ),
            )
        )
        per_home_stats.append(stats)
    (
        events,
        observations,
        selected_root_groups,
        namespace_error_codes,
        namespace_error_count,
        global_excluded_by_limit,
        excluded_by_namespace,
    ) = (
        _combine_and_limit_namespaced_session_data(
            home_batches,
            limit_root_groups=limit_sessions,
        )
    )
    if _session_observations is not None:
        _session_observations.extend(observations)
    if source_plan.omitted_home_count:
        namespace_error_codes.append("source_home_plan_truncated")
        namespace_error_count += source_plan.omitted_home_count
    _merge_multi_home_discovery_stats(
        _discovery_stats,
        per_home_stats,
        events=events,
        limit_unit="root_groups",
        selected_root_groups=selected_root_groups,
        extra_error_codes=namespace_error_codes,
        extra_error_count=namespace_error_count,
        extra_excluded_by_limit=global_excluded_by_limit,
        excluded_by_namespace=excluded_by_namespace,
        observations=observations,
    )
    return events


def _is_claude_workflow_journal(path: Path, projects_root: Path) -> bool:
    """True only for Claude's workflow metadata journal, not a transcript.

    Claude Code stores these at exactly
    ``<project>/<root>/subagents/workflows/wf_*/journal.jsonl``. They contain
    workflow bookkeeping rather than assistant transcript usage. Treating the
    shared ``journal`` stem as a session identity lets the metadata cohort
    consume a real root-group slot and, once a journal exceeds the bounded
    identity prefix, permanently degrades every refresh.
    """

    try:
        parts = path.relative_to(projects_root).parts
    except ValueError:
        return False
    return bool(
        len(parts) == 6
        and parts[-1] == "journal.jsonl"
        and parts[-2].startswith("wf_")
        and parts[-3] == "workflows"
        and parts[-4] == "subagents"
    )


def _claude_file_fingerprint(file_stat: os.stat_result) -> _ClaudeFileFingerprint:
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
    )


def _claude_fingerprint_matches(
    expected: _ClaudeFileFingerprint,
    observed: _ClaudeFileFingerprint,
) -> bool:
    """Require one immutable file snapshot across identity and usage passes."""

    return observed == expected


def _directory_tree_fingerprint(
    observed: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        int(observed.st_dev),
        int(observed.st_ino),
        stat.S_IFMT(observed.st_mode),
        int(observed.st_size),
        int(observed.st_mtime_ns),
        int(observed.st_ctime_ns),
    )


def _open_directory_root_fd_no_follow(root: Path) -> tuple[int, os.stat_result]:
    """Open an absolute directory one component at a time without symlinks."""

    path = Path(os.path.abspath(os.fspath(root.expanduser())))
    if not path.is_absolute():
        raise OSError("source directory is not absolute")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current_fd = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(
                component,
                directory_flags,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        root_stat = os.fstat(current_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise OSError("source directory is not a directory")
        return current_fd, root_stat
    except BaseException:
        os.close(current_fd)
        raise


def _discover_source_tree_files_no_follow(
    root: Path,
    *,
    root_fd: int,
    include_file: Callable[[str], bool],
    unsafe_code: str,
    traversal_code: str,
    changed_code: str,
    skipped_dir_symlinks: list[Path] | None = None,
) -> list[_NoFollowTreeFile]:
    """Exhaustively enumerate a source tree without following or hiding errors.

    ``Path.rglob`` and ``os.walk`` may omit an unreadable or concurrently
    replaced subtree without surfacing an exception.  A full-history source
    cannot claim completeness in that state.  This walk holds every traversed
    directory by descriptor, rejects directory symlinks, and verifies each
    carrier child before and after descent.
    """

    root_path = Path(os.path.abspath(os.fspath(root.expanduser())))
    candidates: list[_NoFollowTreeFile] = []
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )

    def changed() -> _ClientUsageDiscoveryReadError:
        return _ClientUsageDiscoveryReadError(changed_code)

    def walk(directory_fd: int, relative: Path) -> None:
        before = _directory_tree_fingerprint(os.fstat(directory_fd))
        try:
            with os.scandir(directory_fd) as iterator:
                entries = sorted(list(iterator), key=lambda entry: entry.name)
        except OSError as exc:
            raise _ClientUsageDiscoveryReadError(traversal_code) from exc
        for entry in entries:
            name = entry.name
            if not name or name in {".", ".."} or "/" in name:
                raise _ClientUsageDiscoveryReadError(unsafe_code)
            included = include_file(name)
            try:
                first = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise _ClientUsageDiscoveryReadError(traversal_code) from exc
            if stat.S_ISLNK(first.st_mode):
                if included:
                    candidates.append(
                        _NoFollowTreeFile(
                            path=root_path / relative / name,
                            fingerprint=None,
                        )
                    )
                    continue
                # A non-carrier symlink must not condemn the whole walk: the
                # previous fwalk-based scanner silently ignored file/broken
                # symlinks, and a stray "latest"-style link would otherwise
                # zero out the entire home's discovery forever.
                try:
                    target = os.stat(name, dir_fd=directory_fd, follow_symlinks=True)
                except OSError:
                    continue
                if stat.S_ISDIR(target.st_mode):
                    # A directory symlink is never FOLLOWED — descending it
                    # could smuggle an unbounded foreign subtree into a
                    # "complete" scan. But a single such link must not condemn
                    # the whole tree either. Callers that pass
                    # ``skipped_dir_symlinks`` record the skip and keep
                    # enumerating every legitimate sibling; without it the link
                    # stays fatal (unchanged for callers that require closure).
                    # One stray `memory -> <shared dir>` link under
                    # ~/.claude/projects previously zeroed a home's 6000+
                    # transcripts on import (issue #84).
                    if skipped_dir_symlinks is not None:
                        skipped_dir_symlinks.append(root_path / relative / name)
                        continue
                    raise _ClientUsageDiscoveryReadError(unsafe_code)
                continue
            child_relative = relative / name
            if stat.S_ISDIR(first.st_mode):
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise _ClientUsageDiscoveryReadError(traversal_code) from exc
                try:
                    if _directory_tree_fingerprint(
                        os.fstat(child_fd)
                    ) != _directory_tree_fingerprint(first):
                        raise changed()
                    walk(child_fd, child_relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(first.st_mode) and included:
                try:
                    file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise _ClientUsageDiscoveryReadError(traversal_code) from exc
                try:
                    opened = os.fstat(file_fd)
                    if not stat.S_ISREG(opened.st_mode):
                        raise _ClientUsageDiscoveryReadError(unsafe_code)
                    if _claude_file_fingerprint(
                        opened
                    ) != _claude_file_fingerprint(first):
                        raise changed()
                finally:
                    os.close(file_fd)
            elif included:
                candidates.append(
                    _NoFollowTreeFile(
                        path=root_path / child_relative,
                        fingerprint=None,
                    )
                )
            else:
                # Content churn in a regular, non-carrier side file does not
                # affect transcript completeness.  Parent directory identity
                # still detects rename/add/remove races that could hide a
                # subtree.
                continue
            try:
                final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise changed() from exc
            if _directory_tree_fingerprint(
                final
            ) != _directory_tree_fingerprint(first):
                raise changed()
            if stat.S_ISREG(final.st_mode) and included:
                candidates.append(
                    _NoFollowTreeFile(
                        path=root_path / child_relative,
                        fingerprint=_claude_file_fingerprint(final),
                    )
                )
        if _directory_tree_fingerprint(os.fstat(directory_fd)) != before:
            raise changed()

    walk(root_fd, Path())
    return candidates


def _open_claude_projects_root_fd(projects_root: Path) -> tuple[int, os.stat_result]:
    """Open an absolute projects root one component at a time, never via symlink."""

    root = Path(os.path.abspath(os.fspath(projects_root.expanduser())))
    if not root.is_absolute():
        raise _ClaudeTranscriptUnsafePathError("claude projects root is not absolute")
    try:
        return _open_directory_root_fd_no_follow(root)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _ClaudeTranscriptUnsafePathError(
                "claude projects root contains a symlink"
            ) from exc
        raise


def _discover_claude_transcript_paths(
    projects_root: Path,
    *,
    projects_root_fd: int,
    skipped_dir_symlinks: list[Path] | None = None,
) -> list[_NoFollowTreeFile]:
    """Enumerate JSONL candidates from the held source-root descriptor."""

    return _discover_source_tree_files_no_follow(
        projects_root,
        root_fd=projects_root_fd,
        include_file=lambda name: name.endswith(".jsonl"),
        unsafe_code="claude_transcript_unsafe_path",
        traversal_code="claude_transcript_discovery_failed",
        changed_code="claude_transcript_changed_during_scan",
        skipped_dir_symlinks=skipped_dir_symlinks,
    )


def _open_claude_transcript_fd(
    path: Path,
    *,
    projects_root: Path,
    projects_root_fd: int | None = None,
) -> tuple[int, os.stat_result]:
    """Open one regular transcript beneath ``projects_root`` without symlinks.

    Every descendant component is opened relative to an already-open parent
    with ``O_NOFOLLOW``. This prevents a file or directory symlink from
    importing data outside the configured Claude home under that home's source
    namespace. The returned descriptor is owner-held by the caller.
    """

    root = Path(os.path.abspath(os.fspath(projects_root.expanduser())))
    candidate = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise _ClaudeTranscriptUnsafePathError("claude transcript escaped projects root") from exc
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _ClaudeTranscriptUnsafePathError("claude transcript path is invalid")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_fds: list[int] = []
    try:
        if projects_root_fd is None:
            root_fd, _root_stat = _open_claude_projects_root_fd(root)
            directory_fds.append(root_fd)
        else:
            directory_fds.append(os.dup(projects_root_fd))
        for component in parts[:-1]:
            directory_fds.append(
                os.open(
                    component,
                    directory_flags | close_on_exec,
                    dir_fd=directory_fds[-1],
                )
            )
        file_fd = os.open(
            parts[-1],
            file_flags | close_on_exec,
            dir_fd=directory_fds[-1],
        )
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            raise _ClaudeTranscriptUnsafePathError("claude transcript is not a regular file")
        return file_fd, file_stat
    except _ClaudeTranscriptUnsafePathError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _ClaudeTranscriptUnsafePathError("claude transcript path contains a symlink") from exc
        raise
    finally:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _claude_transcript_stat(
    path: Path,
    *,
    projects_root: Path,
    projects_root_fd: int | None = None,
) -> tuple[float, _ClaudeFileFingerprint]:
    file_fd, file_stat = _open_claude_transcript_fd(
        path,
        projects_root=projects_root,
        projects_root_fd=projects_root_fd,
    )
    try:
        return float(file_stat.st_mtime), _claude_file_fingerprint(file_stat)
    finally:
        os.close(file_fd)


def _validate_claude_workflow_journal(
    path: Path,
    *,
    projects_root: Path,
    projects_root_fd: int,
) -> _ClaudeFileFingerprint:
    """Prove an exact workflow journal is metadata-only before ignoring it."""

    file_fd, file_stat = _open_claude_transcript_fd(
        path,
        projects_root=projects_root,
        projects_root_fd=projects_root_fd,
    )
    fingerprint = _claude_file_fingerprint(file_stat)
    remaining = _CLAUDE_WORKFLOW_JOURNAL_MAX_BYTES
    with os.fdopen(file_fd, "rb") as handle:
        for _line_index in range(_CLAUDE_WORKFLOW_JOURNAL_MAX_LINES):
            line = handle.readline(remaining + 1)
            if not line:
                break
            if len(line) > remaining:
                raise _ClientUsageDiscoveryReadError(
                    "claude_workflow_journal_validation_truncated"
                )
            remaining -= len(line)
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise _ClientUsageDiscoveryReadError(
                    "claude_workflow_journal_schema_drift"
                ) from exc
            if not isinstance(obj, dict):
                raise _ClientUsageDiscoveryReadError(
                    "claude_workflow_journal_schema_drift"
                )
            keys = set(obj)
            row_type = obj.get("type")
            if not (
                (row_type == "started" and keys == {"agentId", "key", "type"})
                or (
                    row_type == "result"
                    and keys == {"agentId", "key", "result", "type"}
                )
            ):
                raise _ClientUsageDiscoveryReadError(
                    "claude_workflow_journal_schema_drift"
                )
            if remaining <= 0:
                raise _ClientUsageDiscoveryReadError(
                    "claude_workflow_journal_validation_truncated"
                )
        else:
            if handle.read(1):
                raise _ClientUsageDiscoveryReadError(
                    "claude_workflow_journal_validation_truncated"
                )
        end_fingerprint = _claude_file_fingerprint(os.fstat(handle.fileno()))
    if end_fingerprint != fingerprint:
        raise _ClaudeTranscriptChangedDuringScanError(
            "claude workflow journal changed during validation"
        )
    return fingerprint


def _discover_claude_code_usage_from_home(
    *,
    claude_home: Path,
    limit_sessions: int = 20,
    _discovery_stats: dict[str, Any] | None = None,
    projects_root_fd: int | None = None,
    _session_observations: list[ClientSessionObservation] | None = None,
) -> list[ClientUsageEvent]:
    """Read Claude Code local project JSONL files and return usage summaries."""

    root = claude_home.expanduser()
    projects_root = root / "projects"
    if _discovery_stats is not None:
        _discovery_stats.update(_initial_claude_discovery_stats())
        # The held, no-follow directory descriptor is the authority carrier.
        # Other files in a Claude home do not prove that the transcript source
        # exists.
        _discovery_stats["source_present"] = projects_root_fd is not None
    if projects_root_fd is None:
        if not os.path.lexists(projects_root):
            return []
        raise _ClaudeTranscriptUnsafePathError(
            "claude projects root was not opened from its source boundary"
        )
    skipped_dir_symlinks: list[Path] = []
    try:
        candidate_files = _discover_claude_transcript_paths(
            projects_root,
            projects_root_fd=projects_root_fd,
            skipped_dir_symlinks=skipped_dir_symlinks,
        )
    except _ClaudeTranscriptUnsafePathError:
        raise
    except OSError as exc:
        raise _ClientUsageDiscoveryReadError("claude_transcript_discovery_failed") from exc
    if _discovery_stats is not None:
        _discovery_stats["discovered"] = len(candidate_files)
    workflow_journal_fingerprints: dict[Path, _ClaudeFileFingerprint] = {}
    transcript_paths: list[Path] = []
    traversal_fingerprints: dict[Path, _ClaudeFileFingerprint] = {}
    for candidate in candidate_files:
        path = candidate.path
        traversal_fingerprints[path] = candidate.fingerprint
        if _is_claude_workflow_journal(path, projects_root):
            observed_fingerprint = _validate_claude_workflow_journal(
                path,
                projects_root=projects_root,
                projects_root_fd=projects_root_fd,
            )
            if (
                candidate.fingerprint is None
                or observed_fingerprint != candidate.fingerprint
            ):
                raise _ClaudeTranscriptChangedDuringScanError(
                    "claude workflow journal changed after tree inventory"
                )
            workflow_journal_fingerprints[path] = observed_fingerprint
        else:
            transcript_paths.append(path)
    ignored_non_transcript_files = len(workflow_journal_fingerprints)
    error_count = 0
    error_codes: list[str] = []

    def record_error(code: str) -> None:
        nonlocal error_count
        error_count += 1
        if code not in error_codes:
            error_codes.append(code)

    # A descendant directory symlink is never followed (no-follow policy), but
    # it no longer aborts the whole home. Surface each skipped link as an unsafe
    # path — exactly as a symlinked transcript FILE is — so the skip stays
    # visible in diagnostics while every legitimate sibling still imports
    # (issue #84).
    for _skipped_symlink in skipped_dir_symlinks:
        record_error("claude_transcript_unsafe_path")

    path_entries: list[tuple[Path, float]] = []
    fingerprints: dict[Path, _ClaudeFileFingerprint] = {}
    for path in transcript_paths:
        try:
            mtime, fingerprint = _claude_transcript_stat(
                path,
                projects_root=projects_root,
                projects_root_fd=projects_root_fd,
            )
            if (
                traversal_fingerprints[path] is not None
                and fingerprint != traversal_fingerprints[path]
            ):
                raise _ClaudeTranscriptChangedDuringScanError(
                    "claude transcript changed after tree inventory"
                )
            path_entries.append((path, mtime))
            fingerprints[path] = fingerprint
        except _ClaudeTranscriptUnsafePathError:
            record_error("claude_transcript_unsafe_path")
        except OSError:
            record_error("claude_transcript_stat_failed")

    identity_entries: list[tuple[Path, float, str, int, bool]] = []
    for path, mtime in path_entries:
        try:
            session_id, identity_scan_complete = _peek_claude_session_id(
                path,
                projects_root=projects_root,
                projects_root_fd=projects_root_fd,
                expected_fingerprint=fingerprints[path],
            )
        except _ClaudeTranscriptUnsafePathError:
            record_error("claude_transcript_unsafe_path")
            identity_entries.append((path, mtime, f"unsafe:{path}", 0, False))
            continue
        except _ClaudeTranscriptChangedDuringScanError:
            record_error("claude_transcript_changed_during_scan")
            identity_entries.append((path, mtime, f"changed:{path}", 0, False))
            continue
        except OSError:
            record_error("claude_transcript_read_failed")
            # An unreadable identity cannot safely enter selection: its real
            # root could otherwise be split from a selected parent/child
            # cohort, defeating cross-file replay deduplication.
            identity_entries.append((path, mtime, f"unreadable:{path}", 0, False))
            continue
        if not identity_scan_complete:
            record_error("claude_transcript_identity_scan_truncated")
            # Never fall back to ``path.stem`` for a budget-unresolved file.
            # A late child identity would become a fake root, rotate through
            # the bounded window, and could be persisted in addition to its
            # replaying parent on a later refresh.
            identity_entries.append((path, mtime, f"unresolved:{path}", 0, False))
            continue
        root_session_id = session_id or path.stem
        identity_entries.append(
            (
                path,
                mtime,
                root_session_id,
                0 if path.stem == root_session_id else 1,
                True,
            )
        )
    entries, selected_root_groups = _select_claude_root_groups(
        identity_entries,
        limit_root_groups=limit_sessions,
    )
    events: list[ClientUsageEvent] = []
    observations: list[ClientSessionObservation] = []
    seen_usage_outputs: dict[str, int] = {}
    parsed_transcript_files = 0
    # issue #53: the identity failures above (stat/read/unsafe/scan-truncated)
    # are on files that are EXCLUDED from selection and are already reported via
    # diagnostics (error_count, error_codes, unresolved_identity_files). They must
    # NOT retroactively withhold the cleanly-parsed rows of unrelated SELECTED
    # sessions — otherwise one stray unreadable/non-transcript file (e.g. an
    # unrecognized workflow journal, or a transient stat failure) silently zeroes
    # the whole source's import. Cohort completeness reflects only the selected
    # files actually parsed below.
    selected_cohort_complete = True
    _claude_total_files = len(entries)
    for _claude_index, (path, mtime, _root_session_id, _parent_sort_key, identity_readable) in enumerate(entries):
        if _claude_index % _PROGRESS_EMIT_STRIDE == 0 or _claude_index + 1 == _claude_total_files:
            _emit_scan_progress("claude-code", _claude_index + 1, _claude_total_files)
        if not identity_readable:
            continue
        parse_stats: dict[str, int] = {
            "unparseable_transcripts": 0,
            "malformed_transcript_lines": 0,
            "invalid_usage_rows": 0,
        }
        observation_metadata: dict[str, Any] = {}
        try:
            usages = _read_claude_project_usages(
                path,
                seen_usage_outputs=seen_usage_outputs,
                _parse_stats=parse_stats,
                _observation_metadata=observation_metadata,
                projects_root=projects_root,
                projects_root_fd=projects_root_fd,
                expected_fingerprint=fingerprints[path],
            )
        except _ClaudeTranscriptUnsafePathError:
            selected_cohort_complete = False
            record_error("claude_transcript_unsafe_path")
            continue
        except _ClaudeTranscriptChangedDuringScanError:
            # A transcript rewritten mid-read (a live session appending to it during
            # a manual import) is a transient race: the file is skipped and re-read
            # cleanly next scan. Dedup is transactional (this file staged nothing
            # into the shared map before raising), so skipping it cannot under-count
            # a sibling — no need to withhold the whole cohort. read_failed and
            # unsafe_path stay conservative (a genuine read/security failure).
            record_error("claude_transcript_changed_during_scan")
            continue
        except OSError:
            selected_cohort_complete = False
            record_error("claude_transcript_read_failed")
            continue
        if parse_stats["unparseable_transcripts"]:
            selected_cohort_complete = False
            record_error("claude_transcript_unparseable")
        if parse_stats["malformed_transcript_lines"]:
            selected_cohort_complete = False
            record_error("claude_transcript_malformed_lines")
        if parse_stats["invalid_usage_rows"]:
            selected_cohort_complete = False
            record_error("claude_transcript_usage_schema_drift")
        raw_session_id = str(observation_metadata.get("session_id") or path.stem)
        is_child_transcript = path.stem != raw_session_id
        session_id = _client_session_id_for_file(raw_session_id, path)
        observation_parse_complete = not bool(
            parse_stats["unparseable_transcripts"]
            or parse_stats["malformed_transcript_lines"]
            or parse_stats["invalid_usage_rows"]
        )
        if observation_metadata.get("saw_valid_object"):
            parsed_transcript_files += 1
            observations.append(
                ClientSessionObservation(
                    client="claude-code",
                    client_session_id=session_id,
                    source_path=path,
                    title=_sanitized_session_title(
                        observation_metadata.get("title")
                    ),
                    cwd=_limited_optional_text(
                        observation_metadata.get("cwd"),
                        240,
                    ),
                    observed_models=tuple(
                        model
                        for value in observation_metadata.get("observed_models") or []
                        if (model := _limited_optional_text(value, 120)) is not None
                    ),
                    started_at=_optional_int(
                        observation_metadata.get("first_activity_at")
                    ),
                    updated_at=(
                        _optional_int(observation_metadata.get("last_activity_at"))
                        or _optional_int(mtime)
                    ),
                    source_revision_at=fingerprints[path][3],
                    source_revision_basis="file_mtime_ns",
                    client_session_kind=(
                        "child" if is_child_transcript else "root"
                    ),
                    parent_client_session_id=(
                        raw_session_id if is_child_transcript else None
                    ),
                    client_transcript_id=path.stem,
                    observation_basis="claude_transcript_identity",
                    activity_time_basis=(
                        "transcript_event_time"
                        if observation_metadata.get("last_activity_at") is not None
                        else "file_mtime"
                    ),
                    evidenced_event_ids=tuple(
                        _safe_evidenced_ids(
                            observation_metadata.get("evidenced_event_ids")
                        )
                    ),
                    evidenced_event_id_total=_safe_nonnegative_int(
                        observation_metadata.get("evidenced_event_id_total")
                    ),
                    evidenced_outputs_skipped=_safe_nonnegative_int(
                        observation_metadata.get("evidenced_outputs_skipped")
                    ),
                    refused_recording_attempts=_safe_refused_recording_attempts(
                        observation_metadata.get("refused_recording_attempts")
                    ),
                    source_parse_complete=observation_parse_complete,
                )
            )
        for usage in usages:
            # Stable session key: one client session keeps ONE client_session_id
            # across mid-session model switches. Per-model breakdown lives in
            # usage_row_lane (always set for claude-code rows, single- or
            # multi-model) so row identity stays stable across the
            # single-model -> multi-model transition.
            events.append(
                ClientUsageEvent(
                    client="claude-code",
                    client_session_id=session_id,
                    source_path=path,
                    title=_sanitized_session_title(usage.get("title")),
                    cwd=_limited_optional_text(usage.get("cwd"), 240),
                    model=_limited_optional_text(usage.get("model"), 120),
                    input_tokens=_safe_nonnegative_int(usage.get("input_tokens")),
                    output_tokens=_safe_nonnegative_int(usage.get("output_tokens")),
                    cached_input_tokens=_safe_nonnegative_int(usage.get("cache_creation_input_tokens"))
                    + _safe_nonnegative_int(usage.get("cache_read_input_tokens")),
                    cache_creation_input_tokens=_safe_nonnegative_int(usage.get("cache_creation_input_tokens")),
                    cache_read_input_tokens=_safe_nonnegative_int(usage.get("cache_read_input_tokens")),
                    cache_creation_tokens_reported=True,
                    cache_read_tokens_reported=True,
                    cache_creation_5m_input_tokens=_safe_nonnegative_int(usage.get("cache_creation_5m_input_tokens")),
                    cache_creation_1h_input_tokens=_safe_nonnegative_int(usage.get("cache_creation_1h_input_tokens")),
                    reasoning_output_tokens=0,
                    started_at=_optional_int(usage.get("started_at")),
                    updated_at=_optional_int(usage.get("updated_at")) or _optional_int(mtime),
                    # Per-session transcript file: its ns mtime (emitted as
                    # us) orders two real revisions that share one displayed
                    # second, which the whole-second updated_at cannot.
                    source_revision_at=int(fingerprints[path][3]) // 1000,
                    source_revision_basis="transcript_file_mtime_us",
                    turn_count=_safe_nonnegative_int(usage.get("turn_count")),
                    client_session_kind="child" if is_child_transcript else "root",
                    parent_client_session_id=raw_session_id if is_child_transcript else None,
                    client_transcript_id=path.stem,
                    raw_usage_rows=_safe_nonnegative_int(usage.get("raw_usage_rows")),
                    deduplicated_usage_rows=_safe_nonnegative_int(usage.get("deduplicated_usage_rows")),
                    usage_row_lane=USAGE_ROW_LANE_PREFIX + sanitize_session_key_component(usage.get("model") or "unknown"),
                    evidenced_event_ids=tuple(_safe_evidenced_ids(usage.get("evidenced_event_ids"))),
                    evidenced_event_id_total=_safe_nonnegative_int(usage.get("evidenced_event_id_total")),
                    evidenced_outputs_skipped=_safe_nonnegative_int(usage.get("evidenced_outputs_skipped")),
                    refused_recording_attempts=_safe_refused_recording_attempts(
                        usage.get("refused_recording_attempts")
                    ),
                    source_parse_complete=selected_cohort_complete and not bool(
                        parse_stats["malformed_transcript_lines"]
                        or parse_stats["invalid_usage_rows"]
                    ),
                )
            )
    for path, expected_fingerprint in workflow_journal_fingerprints.items():
        try:
            _mtime, observed_fingerprint = _claude_transcript_stat(
                path,
                projects_root=projects_root,
                projects_root_fd=projects_root_fd,
            )
        except _ClaudeTranscriptUnsafePathError:
            selected_cohort_complete = False
            record_error("claude_transcript_unsafe_path")
            continue
        except OSError:
            selected_cohort_complete = False
            record_error("claude_transcript_read_failed")
            continue
        if observed_fingerprint != expected_fingerprint:
            # Same transient mid-scan race, on a workflow journal. Journals carry
            # no usage rows, so a changed journal never justified withholding the
            # real sessions around it (issue #53 follow-up).
            record_error("claude_transcript_changed_during_scan")
    if not selected_cohort_complete:
        events = [replace(event, source_parse_complete=False) for event in events]
    if _session_observations is not None:
        _session_observations.extend(observations)
    readable_identity_count = sum(1 for entry in identity_entries if entry[4])
    readable_root_count = len(
        {entry[2] for entry in identity_entries if entry[4]}
    )
    unresolved_identity_files = len(transcript_paths) - readable_identity_count
    if _discovery_stats is not None:
        _discovery_stats.update(
            {
                "discovered": len(candidate_files),
                "parsed": parsed_transcript_files,
                "skipped": max(0, len(entries) - parsed_transcript_files),
                "watermark": max(
                    (
                        value
                        for value in [
                            *(event.updated_at for event in events),
                            *(observation.updated_at for observation in observations),
                        ]
                        if value is not None
                    ),
                    default=None,
                ),
                "selected_root_groups": selected_root_groups,
                "returned_rows": len(events),
                "excluded_by_limit": max(
                    0,
                    readable_root_count - selected_root_groups,
                ),
                "ignored_non_transcript_files": ignored_non_transcript_files,
                "unresolved_identity_files": unresolved_identity_files,
                "unparsed_selected_rows": max(0, len(entries) - parsed_transcript_files),
                "observed_sessions": len(
                    {
                        observation.session_identity
                        for observation in observations
                        if observation.source_parse_complete
                    }
                ),
                "usage_sessions": len(
                    {(event.client, event.client_session_id) for event in events}
                ),
                "sessions_without_usage": len(
                    {
                        observation.session_identity
                        for observation in observations
                        if observation.source_parse_complete
                    }
                    - {(event.client, event.client_session_id) for event in events}
                ),
                "error_count": error_count,
                "error_codes": error_codes,
                "skipped_unsafe_paths": len(skipped_dir_symlinks),
            }
        )
    return events


def _source_home_namespace(
    home: Path,
    *,
    already_canonical: bool = False,
) -> str:
    if already_canonical:
        normalized = os.path.abspath(os.fspath(home))
    else:
        try:
            normalized = str(home.resolve())
        except (OSError, RuntimeError):
            normalized = os.path.abspath(os.fspath(home))
    digest = hashlib.sha256(f"local-client-home-v1\0{normalized}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_source_home(home: Path) -> Path:
    """Freeze a source path before reading so a retargeted symlink cannot relabel data."""

    try:
        return home.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.fspath(home)))


def _record_per_home_discovery_error(stats: dict[str, Any], exc: BaseException) -> None:
    code = _client_usage_discovery_error_code(exc)
    error_codes = [value for value in stats.get("error_codes") or [] if isinstance(value, str)]
    if code not in error_codes:
        error_codes.append(code)
    stats["error_codes"] = error_codes
    stats["error_count"] = _safe_nonnegative_int(stats.get("error_count")) + 1


def _with_source_namespace(
    events: list[ClientUsageEvent],
    namespace: str,
) -> list[ClientUsageEvent]:
    return [replace(event, source_namespace_fingerprint=namespace) for event in events]


def _with_observation_source_namespace(
    observations: list[ClientSessionObservation],
    namespace: str,
) -> list[ClientSessionObservation]:
    return [
        replace(observation, source_namespace_fingerprint=namespace)
        for observation in observations
    ]


def _combine_and_limit_namespaced_session_data(
    home_batches: list[
        tuple[
            str,
            list[ClientUsageEvent],
            list[ClientSessionObservation],
        ]
    ],
    *,
    limit_root_groups: int,
) -> tuple[
    list[ClientUsageEvent],
    list[ClientSessionObservation],
    int,
    list[str],
    int,
    int,
    int,
]:
    """Select one namespace-safe root window for usage and session presence.

    Every usage-bearing session also produces an observation candidate, so
    root selection can use session identity without requiring a usage row.
    This keeps a zero-usage root and its measured children in one bounded
    cohort while preserving the existing per-model usage lanes.
    """

    namespaced_observations = [
        (namespace, observation)
        for namespace, _events, observations in home_batches
        for observation in observations
    ]
    if limit_root_groups <= 0 or not namespaced_observations:
        return [], [], 0, [], 0, 0, 0

    namespaces_by_session: dict[tuple[str, str], set[str]] = {}
    for namespace, observation in namespaced_observations:
        key = (observation.client, observation.client_session_id)
        namespaces_by_session.setdefault(key, set()).add(namespace)
    colliding_session_ids = {
        key
        for key, namespaces in namespaces_by_session.items()
        if len(namespaces) > 1
    }
    invalid_keys: set[tuple[str, str, str]] = {
        (namespace, client, session_id)
        for client, session_id in colliding_session_ids
        for namespace in namespaces_by_session[(client, session_id)]
    }
    parent_namespace_mismatch_keys: set[tuple[str, str, str]] = set()
    for namespace, observation in namespaced_observations:
        parent_id = observation.parent_client_session_id
        if parent_id is None:
            continue
        parent_key = (observation.client, parent_id)
        if parent_key not in namespaces_by_session:
            continue
        if namespace not in namespaces_by_session[parent_key]:
            mismatch = (
                namespace,
                observation.client,
                observation.client_session_id,
            )
            invalid_keys.add(mismatch)
            parent_namespace_mismatch_keys.add(mismatch)

    changed = True
    while changed:
        changed = False
        for namespace, observation in namespaced_observations:
            key = (namespace, observation.client, observation.client_session_id)
            parent_id = observation.parent_client_session_id
            if key in invalid_keys or parent_id is None:
                continue
            if (namespace, observation.client, parent_id) in invalid_keys:
                invalid_keys.add(key)
                changed = True

    latest_observation_by_session: dict[
        tuple[str, str, str], ClientSessionObservation
    ] = {}
    for namespace, observation in namespaced_observations:
        key = (namespace, observation.client, observation.client_session_id)
        if key in invalid_keys:
            continue
        previous = latest_observation_by_session.get(key)
        if previous is None or _session_observation_activity(
            observation
        ) > _session_observation_activity(previous):
            latest_observation_by_session[key] = observation

    session_keys = set(latest_observation_by_session)
    root_by_session: dict[
        tuple[str, str, str], tuple[str, str, str]
    ] = {}

    def resolve_root(
        session_key: tuple[str, str, str],
    ) -> tuple[str, str, str]:
        cached = root_by_session.get(session_key)
        if cached is not None:
            return cached
        trail: list[tuple[str, str, str]] = []
        current = session_key
        while current not in root_by_session:
            if current in trail:
                root = min(trail[trail.index(current) :])
                break
            trail.append(current)
            parent = latest_observation_by_session[current].parent_client_session_id
            parent_key = (
                (current[0], current[1], parent)
                if parent is not None
                else None
            )
            if parent_key is None:
                root = current
                break
            if parent_key not in session_keys:
                root = parent_key
                break
            current = parent_key
        else:
            root = root_by_session[current]
        for visited in trail:
            root_by_session[visited] = root
        return root

    activity_by_root: dict[tuple[str, str, str], int] = {}
    for key, observation in latest_observation_by_session.items():
        root = resolve_root(key)
        activity_by_root[root] = max(
            activity_by_root.get(root, 0),
            _session_observation_activity(observation),
        )
    ordered_roots = sorted(
        activity_by_root,
        key=lambda root: (-activity_by_root[root], root),
    )
    selected_roots = ordered_roots[:limit_root_groups]
    rank = {root: index for index, root in enumerate(selected_roots)}
    selected_session_keys = {
        key
        for key in latest_observation_by_session
        if resolve_root(key) in rank
    }
    selected_observations = [
        observation
        for key, observation in latest_observation_by_session.items()
        if key in selected_session_keys and observation.source_parse_complete
    ]
    selected_observations.sort(
        key=lambda observation: (
            rank[
                resolve_root(
                    (
                        str(observation.source_namespace_fingerprint or ""),
                        observation.client,
                        observation.client_session_id,
                    )
                )
            ],
            0 if observation.client_session_kind == "root" else 1,
            -_session_observation_activity(observation),
            observation.client_session_id,
        )
    )

    latest_usage_by_identity: dict[
        tuple[str, str, str, str], ClientUsageEvent
    ] = {}
    for namespace, events, _observations in home_batches:
        for event in events:
            session_key = (namespace, event.client, event.client_session_id)
            if session_key not in selected_session_keys:
                continue
            identity = (namespace, *event.usage_row_identity)
            previous = latest_usage_by_identity.get(identity)
            if previous is None or _usage_event_activity(event) > _usage_event_activity(
                previous
            ):
                latest_usage_by_identity[identity] = event
    selected_events = list(latest_usage_by_identity.values())
    selected_events.sort(
        key=lambda event: (
            rank[
                resolve_root(
                    (
                        str(event.source_namespace_fingerprint or ""),
                        event.client,
                        event.client_session_id,
                    )
                )
            ],
            0 if event.client_session_kind == "root" else 1,
            -_usage_event_activity(event),
            event.client_session_id,
            event.usage_row_lane or "",
        )
    )

    error_codes: list[str] = []
    if colliding_session_ids:
        error_codes.append("source_namespace_session_collision")
    if parent_namespace_mismatch_keys:
        error_codes.append("source_namespace_parent_mismatch")
    return (
        selected_events,
        selected_observations,
        len(selected_roots),
        error_codes,
        len(colliding_session_ids) + len(parent_namespace_mismatch_keys),
        max(0, len(activity_by_root) - len(selected_roots)),
        len(invalid_keys),
    )


def _session_observation_activity(
    observation: ClientSessionObservation,
) -> int:
    return observation.updated_at or observation.started_at or 0


def _combine_and_limit_namespaced_usage(
    home_batches: list[tuple[str, list[ClientUsageEvent]]],
    *,
    limit_root_groups: int,
) -> tuple[list[ClientUsageEvent], int, list[str], int, int, int]:
    """Bound multi-home results without joining bare ids across source homes.

    Client session ids are authoritative only inside their client-home
    namespace. If the same id appears in multiple homes, or a parent reference
    points at a session found only in another home, the affected lineage is
    excluded fail-closed and surfaced through stable diagnostics.
    """

    namespaced_events = [
        (namespace, event)
        for namespace, events in home_batches
        for event in events
    ]
    if limit_root_groups <= 0 or not namespaced_events:
        return [], 0, [], 0, 0, 0

    namespaces_by_session: dict[str, set[str]] = {}
    for namespace, event in namespaced_events:
        namespaces_by_session.setdefault(event.client_session_id, set()).add(namespace)
    colliding_session_ids = {
        session_id
        for session_id, namespaces in namespaces_by_session.items()
        if len(namespaces) > 1
    }
    invalid_keys: set[tuple[str, str]] = {
        (namespace, session_id)
        for session_id in colliding_session_ids
        for namespace in namespaces_by_session[session_id]
    }
    parent_namespace_mismatch_keys: set[tuple[str, str]] = set()
    for namespace, event in namespaced_events:
        parent_id = event.parent_client_session_id
        if parent_id is None or parent_id not in namespaces_by_session:
            continue
        if namespace not in namespaces_by_session[parent_id]:
            mismatch_key = (namespace, event.client_session_id)
            invalid_keys.add(mismatch_key)
            parent_namespace_mismatch_keys.add(mismatch_key)

    # If an invalid parent has descendants in its own home, exclude the whole
    # affected lineage rather than leaving a child that could later attach to a
    # bare id from another source namespace.
    changed = True
    while changed:
        changed = False
        for namespace, event in namespaced_events:
            key = (namespace, event.client_session_id)
            parent_id = event.parent_client_session_id
            if key in invalid_keys or parent_id is None:
                continue
            if (namespace, parent_id) in invalid_keys:
                invalid_keys.add(key)
                changed = True

    excluded_namespace_identities = {
        (namespace, *event.usage_row_identity)
        for namespace, event in namespaced_events
        if (namespace, event.client_session_id) in invalid_keys
    }

    latest_by_identity: dict[tuple[str, str, str, str], ClientUsageEvent] = {}
    for namespace, event in namespaced_events:
        if (namespace, event.client_session_id) in invalid_keys:
            continue
        identity = (namespace, *event.usage_row_identity)
        previous = latest_by_identity.get(identity)
        if previous is None or _usage_event_activity(event) > _usage_event_activity(previous):
            latest_by_identity[identity] = event
    deduped = [
        (identity[0], event)
        for identity, event in latest_by_identity.items()
    ]

    latest_by_session: dict[tuple[str, str], ClientUsageEvent] = {}
    for namespace, event in deduped:
        session_key = (namespace, event.client_session_id)
        previous = latest_by_session.get(session_key)
        if previous is None or _usage_event_activity(event) > _usage_event_activity(previous):
            latest_by_session[session_key] = event
    session_keys = set(latest_by_session)
    root_by_session: dict[tuple[str, str], tuple[str, str]] = {}

    def resolve_root(session_key: tuple[str, str]) -> tuple[str, str]:
        cached = root_by_session.get(session_key)
        if cached is not None:
            return cached
        trail: list[tuple[str, str]] = []
        current = session_key
        while current not in root_by_session:
            if current in trail:
                root_id = min(trail[trail.index(current) :])
                break
            trail.append(current)
            parent = latest_by_session[current].parent_client_session_id
            parent_key = (current[0], parent) if parent is not None else None
            if parent_key is None:
                root_id = current
                break
            if parent_key not in session_keys:
                # A root transcript may be identity-bearing but legitimately
                # contain no usage row. Its selected children still share that
                # authoritative parent id; keep the absent parent as a virtual
                # root so the global multi-home limit cannot split siblings
                # into fake independent roots and drop one of them.
                root_id = parent_key
                break
            current = parent_key
        else:
            root_id = root_by_session[current]
        for visited in trail:
            root_by_session[visited] = root_id
        return root_id

    activity_by_root: dict[tuple[str, str], int] = {}
    for namespace, event in deduped:
        root = resolve_root((namespace, event.client_session_id))
        activity_by_root[root] = max(activity_by_root.get(root, 0), _usage_event_activity(event))
    ordered_roots = sorted(activity_by_root, key=lambda root: (-activity_by_root[root], root))
    selected_roots = ordered_roots[:limit_root_groups]
    rank = {root: index for index, root in enumerate(selected_roots)}
    selected = [
        (namespace, event)
        for namespace, event in deduped
        if resolve_root((namespace, event.client_session_id)) in rank
    ]
    selected.sort(
        key=lambda item: (
            rank[resolve_root((item[0], item[1].client_session_id))],
            0 if item[1].client_session_kind == "root" else 1,
            -_usage_event_activity(item[1]),
            item[1].client_session_id,
            item[1].usage_row_lane or "",
        )
    )
    error_codes: list[str] = []
    if colliding_session_ids:
        error_codes.append("source_namespace_session_collision")
    if parent_namespace_mismatch_keys:
        error_codes.append("source_namespace_parent_mismatch")
    return (
        [event for _namespace, event in selected],
        len(selected_roots),
        error_codes,
        len(colliding_session_ids) + len(parent_namespace_mismatch_keys),
        max(0, len(activity_by_root) - len(selected_roots)),
        len(excluded_namespace_identities),
    )


def _usage_event_activity(event: ClientUsageEvent) -> int:
    return event.updated_at or event.started_at or 0


def _merge_multi_home_discovery_stats(
    target: dict[str, Any] | None,
    per_home: list[dict[str, Any]],
    *,
    events: list[ClientUsageEvent],
    limit_unit: str,
    selected_root_groups: int | None,
    extra_error_codes: list[str],
    extra_error_count: int,
    extra_excluded_by_limit: int,
    excluded_by_namespace: int,
    observations: list[ClientSessionObservation] | None = None,
) -> None:
    if target is None:
        return
    error_codes: list[str] = []
    for stats in per_home:
        for code in stats.get("error_codes") or []:
            if isinstance(code, str) and code not in error_codes:
                error_codes.append(code)
    for code in extra_error_codes:
        if code not in error_codes:
            error_codes.append(code)
    identity_selected_root_groups = (
        sum(
            _safe_nonnegative_int(stats.get("selected_root_groups"))
            for stats in per_home
        )
        if limit_unit == "root_groups"
        else selected_root_groups
    )
    observed_session_keys = {
        observation.source_session_identity
        for observation in observations or []
    }
    usage_session_keys = {
        event.source_session_identity
        for event in events
    }
    target.update(
        {
            "discovered": sum(_safe_nonnegative_int(stats.get("discovered")) for stats in per_home),
            "parsed": sum(_safe_nonnegative_int(stats.get("parsed")) for stats in per_home),
            "skipped": sum(_safe_nonnegative_int(stats.get("skipped")) for stats in per_home),
            "watermark": max(
                (_optional_int(stats.get("watermark")) or 0 for stats in per_home),
                default=0,
            )
            or None,
            "limit_unit": limit_unit,
            # Selection happens before full usage parsing. Keep that attempted
            # identity-cohort count distinct from the smaller number of roots
            # that ultimately produced a usage row.
            "selected_root_groups": identity_selected_root_groups,
            "returned_root_groups": selected_root_groups,
            "returned_rows": len(events),
            "excluded_by_limit": sum(
                _safe_nonnegative_int(stats.get("excluded_by_limit")) for stats in per_home
            )
            + extra_excluded_by_limit,
            "ignored_non_transcript_files": sum(
                _safe_nonnegative_int(stats.get("ignored_non_transcript_files"))
                for stats in per_home
            ),
            "unresolved_identity_files": sum(
                _safe_nonnegative_int(stats.get("unresolved_identity_files"))
                for stats in per_home
            ),
            "excluded_by_source_namespace": excluded_by_namespace,
            "unparsed_selected_rows": sum(
                _safe_nonnegative_int(stats.get("unparsed_selected_rows")) for stats in per_home
            ),
            "observed_sessions": len(observed_session_keys),
            "usage_sessions": len(usage_session_keys),
            "sessions_without_usage": len(
                observed_session_keys - usage_session_keys
            ),
            "error_count": sum(_safe_nonnegative_int(stats.get("error_count")) for stats in per_home)
            + extra_error_count,
            "error_codes": error_codes,
            # Default Claude discovery legitimately probes both XDG and
            # legacy homes.  One importer-verified carrier is sufficient;
            # absent alternate homes are not missing history.
            "source_present": any(
                stats.get("source_present") is True for stats in per_home
            ),
        }
    )
    # Skipped-symlink accounting is specific to the Claude transcript walk (the
    # only source that opts into skip-and-continue over abort, issue #84). Add
    # it only when a per-home stat actually carries it so sources that keep the
    # stricter fail-closed policy do not gain a spurious always-zero field.
    if any("skipped_unsafe_paths" in stats for stats in per_home):
        target["skipped_unsafe_paths"] = sum(
            _safe_nonnegative_int(stats.get("skipped_unsafe_paths"))
            for stats in per_home
        )


def _client_session_id_for_file(session_id: str, path: Path) -> str:
    """Keep Claude Code agent transcript files distinct when they share a parent session id."""

    if path.stem == session_id:
        return session_id
    return f"{session_id}:{path.stem}"


def _select_claude_root_groups(
    entries: list[tuple[Path, float, str, int, bool]],
    *,
    limit_root_groups: int,
) -> tuple[list[tuple[Path, float, str, int, bool]], int]:
    """Select recent Claude conversations without truncating sidechains.

    The lightweight identity pass supplies ``root_session_id`` for every
    transcript before this function applies the limit. Activity on a sidechain
    makes its complete root group recent, while full usage/evidence parsing is
    still restricted to the selected groups. Parent transcripts sort before
    their sidechains so replay deduplication remains stable.
    """

    if limit_root_groups <= 0 or not entries:
        return [], 0
    activity_by_root: dict[str, float] = {}
    for _path, mtime, root_session_id, _parent_sort_key, readable in entries:
        if not readable:
            continue
        activity_by_root[root_session_id] = max(
            activity_by_root.get(root_session_id, 0.0),
            mtime,
        )
    ordered_roots = sorted(
        activity_by_root,
        key=lambda root_session_id: (
            -activity_by_root[root_session_id],
            root_session_id,
        ),
    )
    selected_roots = ordered_roots[:limit_root_groups]
    root_rank = {root_session_id: index for index, root_session_id in enumerate(selected_roots)}
    selected = [
        entry for entry in entries if entry[4] and entry[2] in root_rank
    ]
    selected.sort(
        key=lambda entry: (
            entry[3],
            root_rank[entry[2]],
            -entry[1],
            str(entry[0]),
        )
    )
    return selected, len(selected_roots)


def _peek_claude_session_id(
    path: Path,
    *,
    projects_root: Path | None = None,
    projects_root_fd: int | None = None,
    expected_fingerprint: _ClaudeFileFingerprint | None = None,
) -> tuple[str | None, bool]:
    """Read a bounded prefix for Claude's root-session identity.

    ``complete`` is false when the prefix budget ended before an identity or
    EOF. The caller then keeps the file as its own fail-closed root group and
    records a stable diagnostic instead of scanning the full transcript during
    the lightweight selection pass.
    """

    remaining = _CLAUDE_IDENTITY_SCAN_MAX_BYTES
    file_fd, file_stat = _open_claude_transcript_fd(
        path,
        projects_root=projects_root or path.parent,
        projects_root_fd=projects_root_fd,
    )
    fingerprint = _claude_file_fingerprint(file_stat)
    if expected_fingerprint is not None and not _claude_fingerprint_matches(
        expected_fingerprint,
        fingerprint,
    ):
        os.close(file_fd)
        raise _ClaudeTranscriptChangedDuringScanError(
            "claude transcript changed during identity scan"
        )
    result: tuple[str | None, bool] = (None, False)
    with os.fdopen(file_fd, "rb") as handle:
        for _line_index in range(_CLAUDE_IDENTITY_SCAN_MAX_LINES):
            line = handle.readline(remaining + 1)
            if not line:
                result = (None, True)
                break
            if len(line) > remaining:
                result = (None, False)
                break
            remaining -= len(line)
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            session_id = obj.get("sessionId")
            if isinstance(session_id, str) and session_id:
                result = (session_id, True)
                break
            if remaining <= 0:
                result = (None, False)
                break
        else:
            result = (None, not bool(handle.read(1)))
        end_fingerprint = _claude_file_fingerprint(os.fstat(handle.fileno()))
    if end_fingerprint != fingerprint:
        raise _ClaudeTranscriptChangedDuringScanError(
            "claude transcript changed during identity read"
        )
    return result


def _opencode_home_roots(opencode_home: Path | None) -> list[Path]:
    """Resolve the OpenCode data home(s) to scan.

    OpenCode itself stores under ``$XDG_DATA_HOME/opencode`` (falling back to
    ``~/.local/share/opencode``). ``OPENCODE_DATA_DIR`` is NOT an opencode
    environment variable — it is a ccusage convention — but agentacct keeps it
    as an optional side-channel override so an operator can point the importer
    at a relocated store without exporting XDG variables.
    """

    if opencode_home is not None:
        return [opencode_home.expanduser()]
    configured = _env_text("OPENCODE_DATA_DIR")
    if configured:
        return [
            Path(value).expanduser()
            for value in configured.split(",")
            if value.strip()
        ]
    xdg_data_home = _env_text("XDG_DATA_HOME")
    base = Path(xdg_data_home).expanduser() if xdg_data_home else Path.home() / ".local" / "share"
    return [base / "opencode"]


def discover_opencode_usage(*, opencode_home: Path | None = None, limit_sessions: int = 20) -> list[ClientUsageEvent]:
    """Read OpenCode local usage and return sanitized per-session summaries.

    Since v1.2.0 OpenCode persists usage in a native SQLite store
    (``opencode*.db``) whose ``session`` table carries an authoritative
    per-session token/cost rollup. When that store is present it is the import
    target; otherwise this falls back to the legacy ``run --format json`` export
    path (newline-delimited events with ``step-finish`` token parts). Neither
    path retains prompts, text parts, tool payloads, or transcript content.
    """

    roots = _opencode_home_roots(opencode_home)
    # OpenCode session ids are not yet proven globally unique across homes.
    # Fail closed before applying a scan limit; otherwise one half of a
    # collision can be truncated away and silently imported as trustworthy.
    if len(roots) > 1:
        return []
    db_sources = sorted(
        (
            source
            for root in roots
            for source in _matching_regular_source_files(
                root,
                patterns=("opencode*.db",),
            )
        ),
        key=lambda source: source.mtime,
        reverse=True,
    )
    if db_sources:
        return discover_opencode_usage_from_db(
            db_sources=db_sources,
            limit_sessions=limit_sessions,
        )
    return _discover_opencode_json_usage(roots=roots, limit_sessions=limit_sessions)


def _discover_opencode_json_usage(
    *, roots: list[Path], limit_sessions: int
) -> list[ClientUsageEvent]:
    """Legacy ``opencode run --format json`` export importer (no native db)."""

    sources = sorted(
        (
            source
            for root in roots
            for source in _matching_regular_source_files(
                root,
                patterns=("*.jsonl", "*.json"),
            )
        ),
        key=lambda source: source.mtime,
        reverse=True,
    )[:limit_sessions]
    events: list[ClientUsageEvent] = []
    for source in sources:
        path = source.path
        usage = _read_opencode_json_usage(source)
        if usage is None:
            continue
        events.append(
            ClientUsageEvent(
                client="opencode",
                client_session_id=str(usage.get("session_id") or path.stem),
                source_path=path,
                title=None,
                cwd=None,
                model=_limited_optional_text(usage.get("model"), 120),
                input_tokens=_safe_nonnegative_int(usage.get("input_tokens")),
                output_tokens=_safe_nonnegative_int(usage.get("output_tokens")),
                cached_input_tokens=_safe_nonnegative_int(usage.get("cached_input_tokens")),
                cache_creation_input_tokens=_safe_nonnegative_int(usage.get("cache_write_tokens")),
                cache_read_input_tokens=_safe_nonnegative_int(usage.get("cache_read_tokens")),
                cache_creation_tokens_reported=bool(usage.get("cache_write_tokens_reported")),
                cache_read_tokens_reported=bool(usage.get("cache_read_tokens_reported")),
                reasoning_output_tokens=_safe_nonnegative_int(usage.get("reasoning_output_tokens")),
                started_at=None,
                updated_at=_optional_int(source.mtime),
                turn_count=_safe_nonnegative_int(usage.get("turn_count")),
                client_reported_cost_usd=_optional_float(usage.get("cost_usd")),
                client_transcript_id=path.stem,
                source_namespace_fingerprint=_source_home_namespace(
                    source.root,
                ),
            )
        )
    return events


_OPENCODE_MODEL_PRICING_SUFFIXES = ("-fast",)


def _normalize_opencode_model_id(model_id: str | None) -> str | None:
    """Strip OpenCode's trailing routing suffix so cost pricing resolves.

    OpenCode reports model ids as a base OpenAI model plus a routing suffix
    (``gpt-5.6-sol-fast`` = ``gpt-5.6-sol`` + ``-fast``). Upstream price tables
    (LiteLLM) key only the base model, so the suffixed form never matches and
    cost falls back to unknown. Normalizing to the base model both recovers the
    real price and is the more accurate model identity. A non-matching id (or
    one that is only the suffix) is returned unchanged.
    """

    if not model_id:
        return model_id
    lowered = model_id.lower()
    for suffix in _OPENCODE_MODEL_PRICING_SUFFIXES:
        if lowered.endswith(suffix) and len(model_id) > len(suffix):
            return model_id[: -len(suffix)]
    return model_id


def _opencode_model_fields(raw_model: Any) -> tuple[str | None, str | None]:
    """Extract (model id, provider id) from an OpenCode ``session.model`` cell.

    OpenCode stores the column as a JSON object ``{"id","providerID","variant"?}``.
    Older/degraded rows may hold a bare model-id string. Anything unparseable
    yields ``(None, None)`` so a malformed cell cannot invent a model label. The
    model id is normalized via ``_normalize_opencode_model_id`` (strips the
    ``-fast`` routing suffix) so cost estimation resolves against the base model.
    """

    if isinstance(raw_model, str):
        text = raw_model.strip()
        if not text:
            return None, None
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return _limited_optional_text(text, 120), None
            if isinstance(parsed, dict):
                model_id = _normalize_opencode_model_id(_limited_optional_text(parsed.get("id"), 120))
                provider = _limited_optional_text(parsed.get("providerID"), 80)
                return model_id, (provider.strip().lower() if provider else None)
            return None, None
        return _normalize_opencode_model_id(_limited_optional_text(text, 120)), None
    if isinstance(raw_model, dict):
        model_id = _normalize_opencode_model_id(_limited_optional_text(raw_model.get("id"), 120))
        provider = _limited_optional_text(raw_model.get("providerID"), 80)
        return model_id, (provider.strip().lower() if provider else None)
    return None, None


def _scan_opencode_part_evidence(
    con: sqlite3.Connection, session_ids: list[str]
) -> dict[str, LogEvidenceAccumulator]:
    """Client-log evidence for the OpenCode sessions being imported.

    Scans the ``part`` table for the agentacct MCP creation-tool calls those
    sessions made and returns one accumulator per session, so the recorded
    events (sections, checks) can be attributed back to the session that created
    them — the OpenCode analogue of the codex/claude client-log pairing.

    Privacy + cost boundary (identical in spirit to the codex/claude readers over
    their own transcripts): the ``part`` table is read ONLY for the
    ≤``limit_sessions`` sessions being imported AND ONLY for rows whose ``data``
    names an agentacct MCP server — a cheap SQL prefilter (``session_id`` set +
    a server-name ``LIKE``) keeps every other tool's payload, every prompt, and
    every message body out of the process entirely; they are never SELECTed,
    parsed, read, or retained. Rows are streamed (no ``fetchall`` of the part
    table). From each matching row ONLY an agentacct ``evt_`` id is extracted,
    and ONLY out of an agentacct CREATION-tool response (allowlist + strict
    single-event shape check in ``log_evidence``). Read/list tools that echo
    other sessions' ids are structurally excluded; a tool's INPUT args are never
    read. A part still in flight (no string output) donates nothing and is
    counted as an honest skip.
    """

    evidence: dict[str, LogEvidenceAccumulator] = {}
    if not session_ids:
        return evidence
    if not {"session_id", "data"} <= _sqlite_table_columns(con, "part"):
        return evidence
    session_placeholders = ", ".join("?" for _ in session_ids)
    # Coarse SQL prefilter: only rows whose data references an agentacct-family
    # server name reach Python. The server keys carry no LIKE metacharacters, so
    # a bare ``%key%`` is exact-enough; the strict Python allowlist below is the
    # real gate (a stray non-agentacct row that merely mentions the name is
    # dropped without ever being retained). This bounds the scan to the handful
    # of agentacct parts even on the 500-session dashboard path and lets SQLite
    # apply the filter without materializing every tool payload in memory.
    server_keys = sorted(ACCEPTED_SERVER_KEYS)
    server_like = " or ".join("data like ?" for _ in server_keys)
    params = [*session_ids, *(f"%{key}%" for key in server_keys)]
    cursor = con.execute(
        f"select session_id, data from part "
        f"where session_id in ({session_placeholders}) and ({server_like})",
        params,
    )
    for prow in cursor:
        sid = str(prow["session_id"] or "").strip()
        if not sid:
            continue
        raw = prow["data"]
        if not isinstance(raw, str):
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("type") != "tool":
            continue
        tool = opencode_tool_creation_tool(data.get("tool"))
        if tool is None:
            continue
        state = data.get("state")
        output = state.get("output") if isinstance(state, dict) else None
        acc = evidence.get(sid)
        if acc is None:
            acc = LogEvidenceAccumulator()
            evidence[sid] = acc
        acc.add_output_text(output if isinstance(output, str) else None, tool=tool)
    return evidence


# --- OpenCode Actions + verification extraction (discovery-side) -------------
# OpenCode's observe-only plugin barely fires for its built-in tools (real store
# evidence: ~0 hook ticks), but every tool call it made is on disk in the
# ``part`` table. These pure helpers derive the SAME Actions signals the hook
# path produces (tool categories + names, cwd-relative touched files, best-effort-
# scrubbed commands) AND the one signal OpenCode uniquely affords at discovery
# time: a bash tool's harness ``exit`` code, turned into a mechanical-check tick
# that lifts a step to ``independently_checked`` (Codex's rollout has no clean
# exit code, so its discovery-side verification is deferred). Only tool NAMES,
# cwd-relative touched PATHS, best-effort-scrubbed COMMANDS, and a coarse check
# kind/runner/sha256 digest/exit are derived; never tool output, never a preview,
# never an absolute path prefix.

_OPENCODE_TOOL_PARTS_SCAN_MAX = 5000  # bound accumulation per pathological session
_OPENCODE_CHECK_TICKS_MAX = 200  # bound checks carried per session


def _opencode_edit_paths(part: Mapping[str, Any]) -> list[str]:
    """Every edit-target path one OpenCode edit-family tool part names, as written
    (absolute or relative). Reads three shapes: the direct ``filePath`` (native
    edit/write), the structured ``apply_patch`` metadata ``files[].filePath``, and
    the raw ``apply_patch`` body (same ``*** Add/Update/Delete File:`` grammar the
    Codex reader parses). Relativization and normalization are the caller's final
    gate; a ``read`` (which touches nothing) is never routed here."""

    paths: list[str] = []
    file_path = part.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        paths.append(file_path)
    files_json = part.get("files_json")
    if isinstance(files_json, str) and files_json.strip():
        try:
            files = json.loads(files_json)
        except (json.JSONDecodeError, ValueError):
            files = None
        if isinstance(files, list):
            for entry in files:
                if not isinstance(entry, Mapping):
                    continue
                candidate = entry.get("filePath") or entry.get("path")
                if isinstance(candidate, str) and candidate.strip():
                    paths.append(candidate)
    paths.extend(_codex_apply_patch_paths(part.get("patch_text")))
    return paths


def _opencode_check_tick(
    part: Mapping[str, Any],
    *,
    session_id: str,
) -> dict[str, Any] | None:
    """A spool-shaped mechanical-check tick from ONE completed OpenCode execute
    part, or ``None`` when the part is not an unambiguous check whose harness exit
    code is trustworthy. Recognizes a check runner with the SAME ``classify_command``
    the hook path uses, and reads the harness ``exit`` code plus a STABLE end
    timestamp from the DB (so the content-derived idempotency key dedupes a
    re-imported check instead of double-recording it). Stores only the coarse check
    kind, runner, and a sha256 digest of the command — never the command text."""

    raw_command = part.get("command")
    if not isinstance(raw_command, str) or not raw_command.strip():
        return None
    if str(part.get("status") or "").strip().lower() != "completed":
        return None
    exit_code = part.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return None
    classified = classify_command(raw_command)
    if classified is None:
        return None
    check_kind, runner = classified
    # OpenCode stamps tool times in MILLISECONDS; the tick timestamp is epoch
    # SECONDS (matching the hook's ``time.time()``), so the check attaches to the
    # step active when it ran and normalize_timestamp does not read year ~58500.
    at_ms = part.get("time_end")
    if isinstance(at_ms, bool) or not isinstance(at_ms, (int, float)):
        at_ms = part.get("row_time")
    if isinstance(at_ms, bool) or not isinstance(at_ms, (int, float)) or float(at_ms) <= 0:
        return None
    return {
        "c": "opencode",
        "s": session_id,
        "k": check_kind,
        "r": runner,
        "d": command_digest(raw_command),
        "x": int(exit_code),
        "t": float(at_ms) / 1000.0,
    }


def _opencode_actions_from_parts(
    parts: list[Mapping[str, Any]],
    *,
    cwd: str | None,
    session_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Derive ``(activity, check_ticks)`` for ONE OpenCode session from its tool
    parts. ``activity`` is the exact metadata shape the hook drain emits (values,
    not keys, so the store's key-based secret redaction can't blank a
    credential-shaped name/path). ``parts`` are ``{tool, status, command,
    exit_code, file_path, patch_text, files_json, time_end, row_time}`` dicts
    already json-extracted from the ``part`` table (never the output/preview blob).
    Pure — no DB, no filesystem."""

    name_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    commands: list[str] = []
    touched: list[str] = []
    check_ticks: list[dict[str, Any]] = []
    for part in parts:
        name = normalize_tool_name(part.get("tool"))
        category: str | None = None
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1
            category = tool_category(name)
            category_counts[category] = category_counts.get(category, 0) + 1
        # Commands + verification checks ride on an execute part's ``command``
        # (OpenCode's ``bash``); a non-execute tool never carries one.
        if isinstance(part.get("command"), str) and part.get("command").strip():
            command = _normalize_command(part.get("command"))
            if (
                command
                and command not in commands
                and len(commands) < _COMMANDS_PER_BATCH_MAX
            ):
                commands.append(command)
            tick = _opencode_check_tick(part, session_id=session_id)
            if tick is not None and len(check_ticks) < _OPENCODE_CHECK_TICKS_MAX:
                check_ticks.append(tick)
        if category == "edit":
            for raw_path in _opencode_edit_paths(part):
                touched_path = _normalize_touched_path(
                    _codex_relativize_touched_path(raw_path, cwd)
                )
                if (
                    touched_path
                    and touched_path not in touched
                    and len(touched) < _TOUCHED_FILES_PER_BATCH_MAX
                ):
                    touched.append(touched_path)
    activity: dict[str, Any] = {}
    if category_counts:
        activity["tool_category_counts"] = dict(sorted(category_counts.items()))
    if name_counts:
        activity["tool_names"] = [
            {"name": name, "count": count}
            for name, count in sorted(name_counts.items())
        ]
    if touched:
        activity["touched_files"] = touched
    if commands:
        activity["commands"] = commands
    return activity, check_ticks


def _scan_opencode_part_actions(
    con: sqlite3.Connection,
    session_ids: list[str],
    cwd_by_session: Mapping[str, str | None],
) -> dict[str, dict[str, Any]]:
    """Derive per-session discovery-side Actions + mechanical checks from the
    ``part`` table for the sessions being imported.

    Privacy + cost boundary: the row's ``data`` blob also holds tool OUTPUT and
    file previews, which are NEVER SELECTed. SQL ``json_extract`` pulls ONLY the
    whitelisted scalar fields — tool name, status, the bash command + its exit, the
    edit target path / patch body / structured file list, and the call end time —
    so output, previews, message bodies, and every other tool's payload never enter
    the process. Rows are streamed (no ``fetchall`` of the part table) and bounded
    per session (``_OPENCODE_TOOL_PARTS_SCAN_MAX``).

    Malformed-JSON tolerance: the OpenCode store is read while OpenCode may be
    mid-write, so a ``data`` cell can be a truncated/partial write. A bare
    ``json_extract`` in the WHERE would RAISE ``OperationalError`` (a
    ``sqlite3.DatabaseError``) on such a row and abort the whole scan — which, being
    uncaught in this path, would zero the client's already-read token/cost/evidence
    rows. So the ``type`` filter is guarded by ``json_valid`` inside a CASE
    (whose evaluation order SQL guarantees, unlike a WHERE AND-term): a malformed or
    NULL ``data`` row yields NULL, fails ``= 'tool'``, and is skipped — matching the
    deliberately-tolerant sibling ``_scan_opencode_part_evidence``. Every surviving
    row is valid JSON, so the SELECT ``json_extract`` fields cannot raise. Returns
    ``{session_id: {"activity": {...}, "checks": [...]}}`` for sessions with a signal.
    """

    result: dict[str, dict[str, Any]] = {}
    if not session_ids:
        return result
    if not {"session_id", "data"} <= _sqlite_table_columns(con, "part"):
        return result
    placeholders = ", ".join("?" for _ in session_ids)
    cursor = con.execute(
        f"""
        select
            session_id                                   as session_id,
            json_extract(data, '$.tool')                 as tool,
            json_extract(data, '$.state.status')         as status,
            json_extract(data, '$.state.input.command')  as command,
            json_extract(data, '$.state.metadata.exit')  as exit_code,
            json_extract(data, '$.state.input.filePath') as file_path,
            json_extract(data, '$.state.input.patchText') as patch_text,
            json_extract(data, '$.state.metadata.files') as files_json,
            json_extract(data, '$.state.time.end')       as time_end,
            time_updated                                 as row_time
        from part
        where session_id in ({placeholders})
          and (
              case when json_valid(data) then json_extract(data, '$.type') end
          ) = 'tool'
        """,
        session_ids,
    )
    parts_by_session: dict[str, list[Mapping[str, Any]]] = {}
    for row in cursor:
        sid = str(row["session_id"] or "").strip()
        if not sid:
            continue
        bucket = parts_by_session.setdefault(sid, [])
        if len(bucket) >= _OPENCODE_TOOL_PARTS_SCAN_MAX:
            continue
        bucket.append(
            {
                "tool": row["tool"],
                "status": row["status"],
                "command": row["command"],
                "exit_code": row["exit_code"],
                "file_path": row["file_path"],
                "patch_text": row["patch_text"],
                "files_json": row["files_json"],
                "time_end": row["time_end"],
                "row_time": row["row_time"],
            }
        )
    for sid, parts in parts_by_session.items():
        activity, checks = _opencode_actions_from_parts(
            parts, cwd=cwd_by_session.get(sid), session_id=sid
        )
        if activity or checks:
            result[sid] = {"activity": activity, "checks": checks}
    return result


def _read_opencode_session_db_usage(
    db_source: _RegularSourceFile,
    *,
    limit_sessions: int,
) -> tuple[
    int,
    list[dict[str, Any]],
    dict[str, LogEvidenceAccumulator],
    dict[str, dict[str, Any]],
]:
    """Read the authoritative ``session`` rollup from one ``opencode*.db``.

    Returns ``(discovered_rows, selected_rows, evidence_by_session,
    actions_by_session)``. The per-session token, cost, model, lineage, directory,
    and timestamp columns of the ``session`` table are read for usage; the ``part``
    table is read SEPARATELY, twice, each with its own whitelist: once via a
    server-name SQL prefilter for the agentacct creation-tool event ids only (see
    ``_scan_opencode_part_evidence``), and once via ``json_extract`` for the
    discovery-side Actions + check fields only (see ``_scan_opencode_part_actions``).
    Neither scan SELECTs tool output, previews, prompts, or message bodies. Raises
    on a corrupt or unreadable database so discovery fails closed rather than
    reporting the store as silently absent.
    """

    con = _connect_regular_source_sqlite_read_only(db_source)
    con.row_factory = sqlite3.Row
    try:
        columns = _sqlite_table_columns(con, "session")
        if "id" not in columns:
            raise _ClientUsageDiscoveryReadError("opencode_session_table_missing")
        required = [
            "id",
            "cost",
            "tokens_input",
            "tokens_output",
            "tokens_reasoning",
            "tokens_cache_read",
            "tokens_cache_write",
            "model",
            "time_created",
            "time_updated",
        ]
        optional = [
            column
            for column in ("parent_id", "directory", "title")
            if column in columns
        ]
        select_columns = [
            column for column in required if column in columns
        ] + optional
        discovered_rows = int(
            con.execute("select count(*) from session").fetchone()[0]
        )
        order_by = (
            "time_updated desc, id asc"
            if "time_updated" in columns
            else "id asc"
        )
        rows = con.execute(
            f"""
            select {", ".join(select_columns)}
            from session
            order by {order_by}
            limit ?
            """,
            (max(0, limit_sessions),),
        ).fetchall()
        session_ids = [
            sid for sid in (str(row["id"] or "").strip() for row in rows) if sid
        ]
        evidence_by_session = _scan_opencode_part_evidence(con, session_ids)
        # The session ``directory`` is the cwd an edit target is relativized
        # against; absent (older/degraded schema) it is None and an absolute path
        # is dropped rather than leaked.
        cwd_by_session: dict[str, str | None] = {
            str(row["id"] or "").strip(): (
                row["directory"] if "directory" in columns else None
            )
            for row in rows
            if str(row["id"] or "").strip()
        }
        actions_by_session = _scan_opencode_part_actions(
            con, session_ids, cwd_by_session
        )
    finally:
        con.close()
    selected: list[dict[str, Any]] = []
    for row in rows:
        usage = dict(row)
        # These counters are NOT NULL columns with a default of 0, so a present
        # column is a measured value (possibly a real zero) — distinct from an
        # absent column on a future/degraded schema.
        usage["input_tokens_reported"] = "tokens_input" in columns
        usage["output_tokens_reported"] = "tokens_output" in columns
        usage["reasoning_output_tokens_reported"] = "tokens_reasoning" in columns
        usage["cache_read_tokens_reported"] = "tokens_cache_read" in columns
        usage["cache_write_tokens_reported"] = "tokens_cache_write" in columns
        selected.append(usage)
    return max(0, discovered_rows), selected, evidence_by_session, actions_by_session


def discover_opencode_usage_from_db(
    *,
    db_sources: list[_RegularSourceFile],
    limit_sessions: int = 20,
) -> list[ClientUsageEvent]:
    """Emit one sanitized usage event per OpenCode ``session`` rollup row.

    Each row is a cumulative per-session snapshot: the ``session`` table stores
    the running token/cost total for the session, so the import is treated as a
    refreshable cumulative snapshot (``opencode_session_rollup``). OpenCode most
    often persists ``cost = 0`` and expects clients to price locally, so cost is
    left for the pricing catalog to recompute from tokens (honest
    ``estimated_from_tokens`` confidence) unless a real nonzero cost is stored.
    """

    events: list[ClientUsageEvent] = []
    seen_sessions: set[tuple[str, str]] = set()
    for db_source in db_sources:
        namespace = _source_home_namespace(db_source.root)
        (
            _discovered,
            selected_rows,
            evidence_by_session,
            actions_by_session,
        ) = _read_opencode_session_db_usage(
            db_source,
            limit_sessions=limit_sessions,
        )
        for usage in selected_rows:
            session_id = str(usage.get("id") or "").strip()
            if not session_id:
                continue
            evidence_fields = (
                evidence_by_session[session_id].as_usage_fields()
                if session_id in evidence_by_session
                else {}
            )
            identity = (namespace, session_id)
            if identity in seen_sessions:
                continue
            model_id, provider_id = _opencode_model_fields(usage.get("model"))
            # OpenCode's ``tokens_input`` is the fresh, uncached prompt input;
            # cache reads/writes are tracked as SEPARATE additive buckets (real
            # rows show cache_read > tokens_input, so cache is not a subset of
            # input the way OpenAI/Codex report it). Store the column verbatim —
            # this also matches the legacy JSON step-finish importer — so the
            # cache buckets are priced once at their own rate, not subtracted.
            fresh_input = _safe_nonnegative_int(usage.get("tokens_input"))
            cache_read = _safe_nonnegative_int(usage.get("tokens_cache_read"))
            cache_write = _safe_nonnegative_int(usage.get("tokens_cache_write"))
            stored_cost = _optional_float(usage.get("cost"))
            reported_cost = stored_cost if stored_cost is not None and stored_cost > 0 else None
            updated_at = _timestamp_seconds(usage.get("time_updated"))
            source_revision_ms = _optional_int(usage.get("time_updated"))
            parent_id = _limited_optional_text(usage.get("parent_id"), 512)
            if parent_id == session_id:
                parent_id = None
            session_actions = actions_by_session.get(session_id)
            event = ClientUsageEvent(
                client="opencode",
                client_session_id=session_id,
                source_path=db_source.path,
                title=usage.get("title"),
                cwd=_limited_optional_text(usage.get("directory"), 240),
                model=model_id,
                provider_name=provider_id,
                input_tokens=fresh_input,
                output_tokens=_safe_nonnegative_int(usage.get("tokens_output")),
                input_tokens_reported=bool(usage.get("input_tokens_reported")),
                output_tokens_reported=bool(usage.get("output_tokens_reported")),
                cached_input_tokens=cache_read + cache_write,
                cache_creation_input_tokens=cache_write,
                cache_read_input_tokens=cache_read,
                cache_creation_tokens_reported=bool(usage.get("cache_write_tokens_reported")),
                cache_read_tokens_reported=bool(usage.get("cache_read_tokens_reported")),
                reasoning_output_tokens=_safe_nonnegative_int(usage.get("tokens_reasoning")),
                reasoning_output_tokens_reported=bool(usage.get("reasoning_output_tokens_reported")),
                started_at=_timestamp_seconds(usage.get("time_created")),
                updated_at=updated_at,
                client_reported_cost_usd=reported_cost,
                client_cost_source="opencode_session_cost" if reported_cost is not None else None,
                client_session_kind="child" if parent_id else "root",
                parent_client_session_id=parent_id,
                client_transcript_id=session_id,
                usage_update_semantics_override="opencode_session_rollup",
                # ``time_updated`` is a genuine per-session revision clock (unlike
                # a shared container mtime), so it can order refreshable
                # revisions. Stored in microseconds for the observation lane.
                source_revision_at=source_revision_ms * 1000 if source_revision_ms is not None else None,
                source_revision_basis="opencode_session_time_updated_us" if source_revision_ms is not None else None,
                source_namespace_fingerprint=namespace,
                evidenced_event_ids=tuple(evidence_fields.get("evidenced_event_ids", ())),
                evidenced_event_id_total=int(evidence_fields.get("evidenced_event_id_total", 0)),
                evidenced_outputs_skipped=int(evidence_fields.get("evidenced_outputs_skipped", 0)),
                # Discovery-side Actions + verification carriers (part-table scan).
                # INTERNAL: the import orchestrator emits them as separate
                # tool_activity_observed / machine_check_observed events; they never
                # enter this event's own sentinel serialization.
                rollout_tool_activity=(
                    (session_actions.get("activity") or None)
                    if session_actions
                    else None
                ),
                rollout_mechanical_checks=(
                    tuple(session_actions.get("checks") or ())
                    if session_actions
                    else ()
                ),
            )
            # A session that recorded agentacct work is worth emitting even if the
            # session table carries no token/cost yet (the recorded work IS the
            # signal), so evidence counts as a reason to keep the row alongside
            # usage/cost. Without this an evidenced-but-tokenless session would be
            # dropped and its sections would never attach.
            if _event_has_usage_or_cost(event) or event.evidenced_event_id_total:
                seen_sessions.add(identity)
                events.append(event)
    return events


def discover_openclaw_usage(*, openclaw_home: Path | None = None, limit_sessions: int = 20) -> list[ClientUsageEvent]:
    """Read OpenClaw JSONL session logs and return sanitized usage summaries."""

    sources = _openclaw_session_paths(openclaw_home)[:limit_sessions]
    events: list[ClientUsageEvent] = []
    for source in sources:
        path = source.path
        usage = _read_openclaw_jsonl_usage(source)
        if usage is None:
            continue
        events.append(
            ClientUsageEvent(
                client="openclaw",
                client_session_id=str(usage.get("session_id") or _openclaw_session_id(path)),
                source_path=path,
                title=None,
                cwd=None,
                model=_limited_optional_text(usage.get("model"), 120),
                input_tokens=_safe_nonnegative_int(usage.get("input_tokens")),
                output_tokens=_safe_nonnegative_int(usage.get("output_tokens")),
                cached_input_tokens=_safe_nonnegative_int(usage.get("cache_read_tokens")) + _safe_nonnegative_int(usage.get("cache_write_tokens")),
                cache_creation_input_tokens=_safe_nonnegative_int(usage.get("cache_write_tokens")),
                cache_read_input_tokens=_safe_nonnegative_int(usage.get("cache_read_tokens")),
                cache_creation_tokens_reported=bool(usage.get("cache_write_tokens_reported")),
                cache_read_tokens_reported=bool(usage.get("cache_read_tokens_reported")),
                reasoning_output_tokens=_safe_nonnegative_int(usage.get("reasoning_tokens")),
                provider_name=_limited_optional_text(usage.get("provider"), 80),
                started_at=_optional_int(usage.get("started_at")),
                updated_at=_optional_int(source.mtime),
                turn_count=_safe_nonnegative_int(usage.get("turn_count")),
                client_reported_cost_usd=_optional_float(usage.get("cost_usd")),
                client_cost_source="openclaw_usage_cost_total" if usage.get("cost_usd") is not None else None,
                client_transcript_id=path.name,
            )
        )
    return events


def discover_hermes_usage(
    *,
    hermes_home: Path | None = None,
    limit_sessions: int = 20,
    _discovery_stats: dict[str, Any] | None = None,
    _session_observations: list[ClientSessionObservation] | None = None,
) -> list[ClientUsageEvent]:
    """Read one trusted Hermes state.db and return sanitized usage summaries.

    Hermes session ids are only unique inside one home.  An explicit home (or
    the single default/env home) therefore defines the source namespace.  A
    comma-separated ``HERMES_HOME`` with distinct or unresolved homes returns
    no rows until the caller selects one with ``--hermes-home``.  Regular-file
    aliases to the same database inode are deduplicated and remain one source.
    """

    stats = {
        "discovered": 0,
        "parsed": 0,
        "skipped": 0,
        "watermark": None,
        "limit_unit": "rows",
        "selected_root_groups": None,
        "returned_rows": 0,
        "excluded_by_limit": 0,
        "ignored_non_transcript_files": 0,
        "unresolved_identity_files": 0,
        "unparsed_selected_rows": 0,
        "observed_sessions": 0,
        "usage_sessions": 0,
        "sessions_without_usage": 0,
        "error_count": 0,
        "error_codes": [],
        "source_present": False,
    }
    if _discovery_stats is not None:
        _discovery_stats.update(stats)

    explicit_carrier_declared = False
    explicit_carrier_inspection_failed = False
    if hermes_home is not None:
        explicit_candidate = hermes_home.expanduser()
        explicit_path = (
            explicit_candidate
            if explicit_candidate.name == "state.db"
            else explicit_candidate / "state.db"
        )
        try:
            explicit_path.lstat()
            explicit_carrier_declared = True
        except FileNotFoundError:
            pass
        except OSError:
            explicit_carrier_inspection_failed = True

    plan = _hermes_state_db_plan(hermes_home)
    if plan.requires_explicit_selection:
        stats["error_count"] = 1
        stats["error_codes"] = [
            "hermes_multiple_source_homes_require_explicit_selection"
        ]
        if _discovery_stats is not None:
            _discovery_stats.update(stats)
        return []
    if not plan.sources:
        if explicit_carrier_declared or explicit_carrier_inspection_failed:
            stats["error_count"] = 1
            stats["error_codes"] = ["hermes_state_db_carrier_unreadable"]
            if _discovery_stats is not None:
                _discovery_stats.update(stats)
        return []

    # A non-ambiguous plan contains exactly one inode.  Keeping this guard
    # explicit prevents a future path-plan change from silently merging raw
    # Hermes ids across homes.
    db_source = plan.sources[0]
    namespace = _source_home_namespace(db_source.root, already_canonical=True)
    try:
        scan = _read_hermes_state_db_usage(
            db_source,
            limit_sessions=limit_sessions,
        )
    except (OSError, sqlite3.DatabaseError) as exc:
        stats["error_count"] = 1
        stats["error_codes"] = [_client_usage_discovery_error_code(exc)]
        if _discovery_stats is not None:
            _discovery_stats.update(stats)
        return []
    # The plan selected exactly one regular state.db and the schema/query was
    # read successfully.  That is stronger evidence than the containing home
    # merely existing, including for a valid database with zero sessions.
    stats["source_present"] = True

    events: list[ClientUsageEvent] = []
    observations: list[ClientSessionObservation] = []
    seen_sessions: set[str] = set()
    for usage in scan.selected_rows:
        session_id = str(usage.get("session_id") or "").strip()
        if not session_id or session_id in seen_sessions:
            continue
        seen_sessions.add(session_id)
        model = _limited_optional_text(usage.get("model"), 120)
        started_at = _timestamp_seconds(usage.get("started_at"))
        observations.append(
            ClientSessionObservation(
                client="hermes",
                client_session_id=session_id,
                source_path=db_source.path,
                # Privacy boundary: Hermes transcript/session titles are not
                # selected from SQLite and can never enter the observation.
                title=None,
                cwd=_limited_optional_text(usage.get("cwd"), 240),
                observed_models=(model,) if model is not None else (),
                started_at=started_at,
                # The database file can be touched by an unrelated newer
                # session.  Never make this row look newly active because the
                # shared container changed; its own started_at is the only
                # row-level activity clock Hermes exposes here.
                updated_at=started_at,
                source_revision_at=db_source.mtime_ns,
                source_revision_basis="state_db_mtime_ns",
                client_transcript_id=session_id,
                observation_basis="hermes_state_db_session_row",
                activity_time_basis="client_started_at",
                source_namespace_fingerprint=namespace,
            )
        )
        event = ClientUsageEvent(
            client="hermes",
            client_session_id=session_id,
            source_path=db_source.path,
            # title stays None by design: never import transcript-derived
            # text, even short labels, without an explicit owner opt-in.
            title=None,
            cwd=_limited_optional_text(usage.get("cwd"), 240),
            model=model,
            input_tokens=_safe_nonnegative_int(usage.get("input_tokens")),
            output_tokens=_safe_nonnegative_int(usage.get("output_tokens")),
            cached_input_tokens=_safe_nonnegative_int(usage.get("cache_read_tokens")) + _safe_nonnegative_int(usage.get("cache_write_tokens")),
            cache_creation_input_tokens=_safe_nonnegative_int(usage.get("cache_write_tokens")),
            cache_read_input_tokens=_safe_nonnegative_int(usage.get("cache_read_tokens")),
            cache_creation_tokens_reported=bool(usage.get("cache_write_tokens_reported")),
            cache_read_tokens_reported=bool(usage.get("cache_read_tokens_reported")),
            reasoning_output_tokens=_safe_nonnegative_int(usage.get("reasoning_tokens")),
            provider_name=_limited_optional_text(usage.get("provider"), 80),
            started_at=started_at,
            # Mirror the observation row above: the shared state.db mtime is a
            # container clock that advances whenever ANY hermes session
            # writes. Using it as this row's watermark rewrote every hermes
            # v1 row and appended one Evidence watermark transition per slot
            # on every import while hermes was open. The row's own started_at
            # is the only per-row activity clock Hermes exposes here.
            updated_at=started_at,
            turn_count=_safe_nonnegative_int(usage.get("message_count")),
            client_reported_cost_usd=_optional_float(usage.get("cost_usd")),
            client_cost_source=_limited_optional_text(usage.get("cost_source"), 80),
            client_transcript_id=session_id,
            source_namespace_fingerprint=namespace,
        )
        if _event_has_usage_or_cost(event):
            events.append(event)

    observation_keys = {
        observation.source_session_identity for observation in observations
    }
    usage_keys = {event.source_session_identity for event in events}
    selected_rows = len(scan.selected_rows)
    stats.update(
        {
            "discovered": scan.discovered_rows,
            "parsed": len(observations),
            "skipped": max(0, selected_rows - len(observations)),
            "watermark": max(
                (
                    value
                    for value in [
                        *(event.updated_at for event in events),
                        *(observation.updated_at for observation in observations),
                    ]
                    if value is not None
                ),
                default=None,
            ),
            "returned_rows": len(events),
            "excluded_by_limit": max(
                0,
                scan.discovered_rows - selected_rows,
            ),
            "unparsed_selected_rows": max(
                0,
                selected_rows - len(observations),
            ),
            "observed_sessions": len(observation_keys),
            "usage_sessions": len(usage_keys),
            "sessions_without_usage": len(observation_keys - usage_keys),
        }
    )
    if _discovery_stats is not None:
        _discovery_stats.update(stats)
    if _session_observations is not None:
        _session_observations.extend(observations)
    return events


_CURSOR_COMPOSER_PREFIX = "composerData:"
_CURSOR_MAX_SECONDS_VALUE = 10_000_000_000
_CURSOR_MIN_MILLISECONDS_VALUE = 100_000_000_000
_CURSOR_MAX_MILLISECONDS_VALUE = 10_000_000_000_000
_CURSOR_MAX_FUTURE_SKEW_SECONDS = 24 * 60 * 60


def _cursor_root(cursor_home: Path | None) -> Path:
    return Path(
        os.path.abspath(
            os.fspath(
                (
                    cursor_home
                    if cursor_home is not None
                    else Path.home()
                    / "Library"
                    / "Application Support"
                    / "Cursor"
                ).expanduser()
            )
        )
    )


def cursor_state_db_path(cursor_home: Path | None = None) -> Path:
    """The one Cursor store this adapter is allowed to inspect."""

    return _cursor_root(cursor_home) / "User" / "globalStorage" / "state.vscdb"


def _cursor_state_db_source(cursor_home: Path | None) -> _RegularSourceFile | None:
    """Resolve Cursor's primary state DB without following any owned symlink."""

    root = _cursor_root(cursor_home)
    user_dir = root / "User"
    global_storage = user_dir / "globalStorage"
    db_path = global_storage / "state.vscdb"
    components = (
        (root, "cursor_root_symlink_not_allowed", "root"),
        (user_dir, "cursor_user_dir_symlink_not_allowed", "directory"),
        (
            global_storage,
            "cursor_global_storage_symlink_not_allowed",
            "directory",
        ),
        (db_path, "cursor_state_db_symlink_not_allowed", "file"),
    )
    for index, (path, symlink_code, expected_kind) in enumerate(components):
        try:
            observed = path.lstat()
        except FileNotFoundError:
            # A missing owned component means this source simply is not
            # installed yet. Never probe backups or similarly named stores.
            return None
        except OSError as exc:
            raise _CursorStateDbReadError(
                "cursor_state_db_filesystem_read_failed"
            ) from exc
        if stat.S_ISLNK(observed.st_mode):
            raise _CursorStateDbReadError(symlink_code)
        if expected_kind in {"root", "directory"} and not stat.S_ISDIR(
            observed.st_mode
        ):
            raise _CursorStateDbReadError("cursor_state_db_path_shape_invalid")
        if expected_kind == "file" and not stat.S_ISREG(observed.st_mode):
            raise _CursorStateDbReadError("cursor_state_db_path_shape_invalid")
        # ``index`` is intentionally consumed so future component additions
        # cannot accidentally skip an intermediate trust check.
        assert index >= 0
    source = _regular_source_file(db_path, root=root)
    if source is None:
        raise _CursorStateDbReadError("cursor_state_db_unsafe_path")
    return source


def _cursor_active_wal_present(source: _RegularSourceFile) -> bool:
    """Validate SQLite sidecars and report whether WAL has real frames.

    Cursor can leave an empty regular ``-wal`` file behind after a clean
    checkpoint. Its zero length proves there is no WAL header/frame for SQLite
    to consult, so it is safe to accept. Symlinked or non-regular WAL/SHM
    sidecars remain unsafe because a read-only connection may inspect either.
    """

    active_wal = False
    for suffix in ("-wal", "-shm"):
        sidecar = source.path.with_name(source.path.name + suffix)
        try:
            observed = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _CursorStateDbReadError(
                "cursor_state_db_filesystem_read_failed"
            ) from exc
        if not stat.S_ISREG(observed.st_mode):
            raise _CursorStateDbReadError(
                "cursor_state_db_sidecar_unsafe"
            )
        if suffix == "-wal" and int(observed.st_size) > 0:
            active_wal = True
    return active_wal


def _assert_cursor_source_unchanged(source: _RegularSourceFile) -> None:
    if _cursor_active_wal_present(source):
        # The adapter deliberately does not use immutable=1 and does not
        # pretend a live WAL is absent. Snapshot/copy support must be designed
        # explicitly before this source can be read while Cursor is writing.
        raise _CursorStateDbReadError("cursor_active_wal_not_supported")
    try:
        observed = source.path.lstat()
    except OSError as exc:
        raise _CursorStateDbReadError(
            "cursor_state_db_replaced_during_scan"
        ) from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or int(observed.st_dev) != source.device
        or int(observed.st_ino) != source.inode
    ):
        raise _CursorStateDbReadError(
            "cursor_state_db_replaced_during_scan"
        )
    if (
        int(observed.st_mtime_ns) != source.mtime_ns
        or int(observed.st_size) != source.size
    ):
        raise _CursorStateDbReadError("cursor_state_db_changed_during_scan")


def _cursor_model_label(value: Any) -> str | None:
    model = _limited_optional_text(value, 120)
    if model is None or not model.strip() or model.strip().lower() == "default":
        return None
    return model.strip()


def _cursor_timestamp_seconds(
    value: Any,
    *,
    now_seconds: float,
) -> tuple[float, str]:
    """Normalize an explicit Cursor seconds/milliseconds timestamp.

    Modern epoch seconds and milliseconds are separated by a deliberately
    large invalid gap. This lets agentacct reject mixed/ambiguous units rather
    than silently dividing an unexpected microsecond or malformed value.
    """

    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise _CursorStateDbReadError(
            "cursor_composer_timestamp_invalid"
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise _CursorStateDbReadError("cursor_composer_timestamp_invalid")
    if number <= _CURSOR_MAX_SECONDS_VALUE:
        seconds = number
        unit = "seconds"
    elif (
        _CURSOR_MIN_MILLISECONDS_VALUE
        <= number
        <= _CURSOR_MAX_MILLISECONDS_VALUE
    ):
        seconds = number / 1000.0
        unit = "milliseconds"
    else:
        raise _CursorStateDbReadError("cursor_composer_timestamp_invalid")
    if seconds > now_seconds + _CURSOR_MAX_FUTURE_SKEW_SECONDS:
        raise _CursorStateDbReadError("cursor_composer_timestamp_invalid")
    return seconds, unit


def _read_cursor_state_db_observations(
    source: _RegularSourceFile,
) -> list[ClientSessionObservation]:
    """Read only allowlisted Cursor composer identity metadata via SQLite.

    The ``value`` JSON blob is never selected into Python. SQLite extracts the
    five scalar/relationship fields this adapter is allowed to retain; prompt,
    message, title, and other composer content stay inside the database.
    """

    _assert_cursor_source_unchanged(source)
    discovered_rows = 0
    con: sqlite3.Connection | None = None
    try:
        con = _connect_regular_source_sqlite_read_only(source)
        con.row_factory = sqlite3.Row
        con.execute("pragma query_only = on")
        # Every allowlisted SELECT belongs to one SQLite read snapshot. The
        # post-read fingerprint still detects an in-place writer so the next
        # scan can retry from one stable client revision.
        con.execute("begin")
        integrity = con.execute("pragma quick_check(1)").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise _CursorStateDbReadError("cursor_state_db_corrupt")
        if not {"key", "value"}.issubset(
            _sqlite_table_columns(con, "cursorDiskKV")
        ):
            raise _CursorStateDbReadError(
                "cursor_state_db_schema_unsupported"
            )
        prefix_length = len(_CURSOR_COMPOSER_PREFIX)
        discovered_rows = int(
            con.execute(
                "select count(*) from cursorDiskKV "
                "where substr(key, 1, ?) = ?",
                (prefix_length, _CURSOR_COMPOSER_PREFIX),
            ).fetchone()[0]
        )
        invalid_json = int(
            con.execute(
                "select count(*) from cursorDiskKV "
                "where substr(key, 1, ?) = ? and not json_valid(value)",
                (prefix_length, _CURSOR_COMPOSER_PREFIX),
            ).fetchone()[0]
        )
        if invalid_json:
            raise _CursorStateDbReadError(
                "cursor_composer_json_invalid",
                discovered_rows=discovered_rows,
            )
        invalid_scalar_shape = int(
            con.execute(
                """
                select count(*) from cursorDiskKV
                where substr(key, 1, ?) = ?
                  and (
                    coalesce(json_type(value, '$.composerId'), 'missing') != 'text'
                    or json_type(value, '$.modelConfig.modelName')
                         not in ('text')
                    or coalesce(json_type(value, '$.createdAt'), 'missing')
                         not in ('integer', 'real')
                    or json_type(value, '$.lastUpdatedAt')
                         not in ('integer', 'real')
                  )
                """,
                (prefix_length, _CURSOR_COMPOSER_PREFIX),
            ).fetchone()[0]
        )
        if invalid_scalar_shape:
            raise _CursorStateDbReadError(
                "cursor_composer_scalar_schema_invalid",
                discovered_rows=discovered_rows,
            )
        invalid_array_shape = int(
            con.execute(
                """
                select count(*) from cursorDiskKV
                where substr(key, 1, ?) = ?
                  and (
                    (json_type(value, '$.subComposerIds') is not null
                     and json_type(value, '$.subComposerIds') != 'array')
                    or
                    (json_type(value, '$.subagentComposerIds') is not null
                     and json_type(value, '$.subagentComposerIds') != 'array')
                  )
                """,
                (prefix_length, _CURSOR_COMPOSER_PREFIX),
            ).fetchone()[0]
        )
        if invalid_array_shape:
            raise _CursorStateDbReadError(
                "cursor_composer_relationship_schema_invalid",
                discovered_rows=discovered_rows,
            )
        rows = con.execute(
            """
            select
              substr(key, ?) as key_composer_id,
              json_extract(value, '$.composerId') as composer_id,
              json_extract(value, '$.createdAt') as created_at,
              json_extract(value, '$.lastUpdatedAt') as last_updated_at,
              json_extract(value, '$.modelConfig.modelName') as model_name
            from cursorDiskKV
            where substr(key, 1, ?) = ?
            """,
            (prefix_length + 1, prefix_length, _CURSOR_COMPOSER_PREFIX),
        ).fetchall()
        edge_rows = con.execute(
            """
            select
              substr(d.key, ?) as parent_id,
              child.value as child_id,
              child.type as child_type
            from cursorDiskKV as d,
                 json_each(json_extract(d.value, '$.subComposerIds')) as child
            where substr(d.key, 1, ?) = ?
            union all
            select
              substr(d.key, ?) as parent_id,
              child.value as child_id,
              child.type as child_type
            from cursorDiskKV as d,
                 json_each(json_extract(d.value, '$.subagentComposerIds')) as child
            where substr(d.key, 1, ?) = ?
            """,
            (
                prefix_length + 1,
                prefix_length,
                _CURSOR_COMPOSER_PREFIX,
                prefix_length + 1,
                prefix_length,
                _CURSOR_COMPOSER_PREFIX,
            ),
        ).fetchall()
        _assert_cursor_source_unchanged(source)
    except _CursorStateDbReadError:
        raise
    except sqlite3.DatabaseError as exc:
        code = (
            "cursor_state_db_schema_unsupported"
            if "no such" in str(exc).lower()
            else "cursor_state_db_corrupt"
        )
        raise _CursorStateDbReadError(
            code,
            discovered_rows=discovered_rows,
        ) from exc
    except OSError as exc:
        raise _CursorStateDbReadError(
            "cursor_state_db_replaced_during_scan",
            discovered_rows=discovered_rows,
        ) from exc
    finally:
        if con is not None:
            con.close()

    metadata_by_id: dict[str, sqlite3.Row] = {}
    for row in rows:
        key_id = str(row["key_composer_id"] or "").strip()
        composer_id = str(row["composer_id"] or "").strip()
        if (
            not key_id
            or len(key_id) > 512
            or composer_id != key_id
            or composer_id in metadata_by_id
        ):
            raise _CursorStateDbReadError(
                "cursor_composer_identity_mismatch",
                discovered_rows=discovered_rows,
            )
        metadata_by_id[composer_id] = row

    parents_by_child: dict[str, set[str]] = {}
    for edge in edge_rows:
        parent_id = str(edge["parent_id"] or "").strip()
        child_id = str(edge["child_id"] or "").strip()
        if (
            edge["child_type"] != "text"
            or not child_id
            or len(child_id) > 512
        ):
            raise _CursorStateDbReadError(
                "cursor_composer_relationship_schema_invalid",
                discovered_rows=discovered_rows,
            )
        if child_id == parent_id:
            raise _CursorStateDbReadError(
                "cursor_composer_self_cycle",
                discovered_rows=discovered_rows,
            )
        if child_id in metadata_by_id:
            parents_by_child.setdefault(child_id, set()).add(parent_id)
    if any(len(parents) > 1 for parents in parents_by_child.values()):
        raise _CursorStateDbReadError(
            "cursor_composer_multiple_parents",
            discovered_rows=discovered_rows,
        )
    parent_by_child = {
        child_id: next(iter(parents))
        for child_id, parents in parents_by_child.items()
    }
    for session_id in metadata_by_id:
        trail: set[str] = set()
        current = session_id
        while current in parent_by_child:
            if current in trail:
                raise _CursorStateDbReadError(
                    "cursor_composer_cycle",
                    discovered_rows=discovered_rows,
                )
            trail.add(current)
            current = parent_by_child[current]

    namespace = _source_home_namespace(source.root, already_canonical=True)
    observations: list[ClientSessionObservation] = []
    scan_now = time.time()
    for composer_id, row in metadata_by_id.items():
        try:
            created_seconds, created_unit = _cursor_timestamp_seconds(
                row["created_at"],
                now_seconds=scan_now,
            )
            if row["last_updated_at"] is None:
                last_updated_seconds = None
            else:
                last_updated_seconds, last_updated_unit = (
                    _cursor_timestamp_seconds(
                        row["last_updated_at"],
                        now_seconds=scan_now,
                    )
                )
                if (
                    last_updated_unit != created_unit
                    or last_updated_seconds < created_seconds
                ):
                    raise _CursorStateDbReadError(
                        "cursor_composer_timestamp_invalid"
                    )
        except _CursorStateDbReadError as exc:
            raise _CursorStateDbReadError(
                exc.code,
                discovered_rows=discovered_rows,
            ) from exc
        created_at = int(created_seconds)
        last_updated_at = (
            int(last_updated_seconds)
            if last_updated_seconds is not None
            else None
        )
        parent_id = parent_by_child.get(composer_id)
        model = _cursor_model_label(row["model_name"])
        observations.append(
            ClientSessionObservation(
                client="cursor",
                client_session_id=composer_id,
                source_path=source.path,
                title=None,
                cwd=None,
                observed_models=(model,) if model is not None else (),
                started_at=created_at,
                updated_at=last_updated_at or created_at,
                source_revision_at=source.mtime_ns,
                source_revision_basis="state_vscdb_mtime_ns",
                client_session_kind="child" if parent_id is not None else "root",
                parent_client_session_id=parent_id,
                client_transcript_id=composer_id,
                client_thread_source="cursor_disk_kv_composer",
                observation_basis="cursor_state_vscdb_composer_identity",
                activity_time_basis="client_composer_metadata",
                source_namespace_fingerprint=namespace,
            )
        )
    return observations


def discover_cursor_usage(
    *,
    cursor_home: Path | None = None,
    limit_sessions: int = 20,
    _discovery_stats: dict[str, Any] | None = None,
    _session_observations: list[ClientSessionObservation] | None = None,
) -> list[ClientUsageEvent]:
    """Discover Cursor composer sessions without claiming usage or cost."""

    stats: dict[str, Any] = {
        "discovered": 0,
        "parsed": 0,
        "skipped": 0,
        "watermark": None,
        "limit_unit": "root_groups",
        "selected_root_groups": 0,
        "returned_root_groups": 0,
        "returned_rows": 0,
        "excluded_by_limit": 0,
        "ignored_non_transcript_files": 0,
        "unresolved_identity_files": 0,
        "unparsed_selected_rows": 0,
        "observed_sessions": 0,
        "usage_sessions": 0,
        "sessions_without_usage": 0,
        "error_count": 0,
        "error_codes": [],
        "source_present": False,
    }
    try:
        source = _cursor_state_db_source(cursor_home)
        if source is None:
            if _discovery_stats is not None:
                _discovery_stats.update(stats)
            return []
        stats["source_present"] = True
        observations = _read_cursor_state_db_observations(source)
        stats["discovered"] = len(observations)
        (
            _events,
            selected_observations,
            selected_root_groups,
            graph_error_codes,
            graph_error_count,
            excluded_by_limit,
            excluded_by_namespace,
        ) = _combine_and_limit_namespaced_session_data(
            [
                (
                    _source_home_namespace(source.root, already_canonical=True),
                    [],
                    observations,
                )
            ],
            limit_root_groups=limit_sessions,
        )
        if graph_error_codes or graph_error_count or excluded_by_namespace:
            raise _CursorStateDbReadError(
                "cursor_composer_namespace_graph_invalid",
                discovered_rows=len(observations),
            )
    except _CursorStateDbReadError as exc:
        stats["discovered"] = exc.discovered_rows
        stats["error_count"] = 1
        stats["error_codes"] = [exc.code]
        if _discovery_stats is not None:
            _discovery_stats.update(stats)
        return []

    selected_keys = {
        observation.source_session_identity
        for observation in selected_observations
    }
    stats.update(
        {
            "parsed": len(selected_observations),
            "watermark": max(
                (
                    _session_observation_activity(observation)
                    for observation in selected_observations
                ),
                default=0,
            )
            or None,
            "selected_root_groups": selected_root_groups,
            "returned_root_groups": selected_root_groups,
            "excluded_by_limit": excluded_by_limit,
            "observed_sessions": len(selected_keys),
            "usage_sessions": 0,
            "sessions_without_usage": len(selected_keys),
        }
    )
    if _session_observations is not None:
        _session_observations.extend(selected_observations)
    if _discovery_stats is not None:
        _discovery_stats.update(stats)
    # Observation-only by contract: never emit a measured zero usage row.
    return []


def discover_client_usage_with_diagnostics(
    *,
    client: str = "all",
    codex_home: Path | None = None,
    claude_home: Path | None = None,
    opencode_home: Path | None = None,
    hermes_home: Path | None = None,
    openclaw_home: Path | None = None,
    cursor_home: Path | None = None,
    limit_sessions: int = 20,
) -> ClientUsageDiscoveryResult:
    """Discover local usage and report what each selected source observed.

    Error diagnostics contain stable codes only. Exception text and source
    paths are deliberately excluded so this result is safe to expose in the
    local product UI. ``discover_client_usage`` remains the compatibility API
    for callers that only need events. ``skipped`` counts selected source units
    that produced no usage row; intentionally unselected history is reported
    separately as ``excluded_by_limit`` so a healthy bounded scan does not look
    like a parser failure.
    """

    candidates: list[ClientUsageEvent] = []
    session_observations: list[ClientSessionObservation] = []
    diagnostics: dict[str, dict[str, Any]] = {}
    for client_name in SUPPORTED_CLIENTS:
        if client not in {"all", client_name}:
            continue
        stats: dict[str, Any] = {}
        error_codes: list[str] = []
        caught_error_count = 0
        try:
            if client_name == "codex":
                discovered_events = discover_codex_usage(
                    codex_home=codex_home,
                    limit_sessions=limit_sessions,
                    _discovery_stats=stats,
                    _session_observations=session_observations,
                )
            elif client_name == "claude-code":
                discovered_events = discover_claude_code_usage(
                    claude_home=claude_home,
                    limit_sessions=limit_sessions,
                    _discovery_stats=stats,
                    _session_observations=session_observations,
                )
            elif client_name == "opencode":
                discovered_events = discover_opencode_usage(
                    opencode_home=opencode_home,
                    limit_sessions=limit_sessions,
                )
            elif client_name == "hermes":
                discovered_events = discover_hermes_usage(
                    hermes_home=hermes_home,
                    limit_sessions=limit_sessions,
                    _discovery_stats=stats,
                    _session_observations=session_observations,
                )
            elif client_name == "cursor":
                discovered_events = discover_cursor_usage(
                    cursor_home=cursor_home,
                    limit_sessions=limit_sessions,
                    _discovery_stats=stats,
                    _session_observations=session_observations,
                )
            else:
                discovered_events = discover_openclaw_usage(
                    openclaw_home=openclaw_home,
                    limit_sessions=limit_sessions,
                )
        except (_ClientUsageDiscoveryReadError, OSError, sqlite3.DatabaseError) as exc:
            discovered_events = []
            error_codes.append(_client_usage_discovery_error_code(exc))
            caught_error_count = 1
        candidates.extend(discovered_events)
        if client_name not in {"codex", "claude-code", "hermes", "cursor"}:
            stats.update(
                {
                    "discovered": len(discovered_events),
                    "parsed": len(discovered_events),
                    "skipped": 0,
                    "watermark": max(
                        (event.updated_at for event in discovered_events if event.updated_at is not None),
                        default=None,
                    ),
                    "limit_unit": "rows",
                    "selected_root_groups": None,
                    "returned_rows": len(discovered_events),
                    "excluded_by_limit": 0,
                    "unparsed_selected_rows": 0,
                    "observed_sessions": len(
                        {
                            (event.client, event.client_session_id)
                            for event in discovered_events
                        }
                    ),
                    "usage_sessions": len(
                        {
                            (event.client, event.client_session_id)
                            for event in discovered_events
                        }
                    ),
                    "sessions_without_usage": 0,
                }
            )
        for error_code in stats.get("error_codes") or []:
            if isinstance(error_code, str) and error_code not in error_codes:
                error_codes.append(error_code)
        diagnostics[client_name] = {
            "client": client_name,
            "discovered": _safe_nonnegative_int(stats.get("discovered")),
            "parsed": _safe_nonnegative_int(stats.get("parsed")),
            "skipped": _safe_nonnegative_int(stats.get("skipped")),
            "error_count": caught_error_count + _safe_nonnegative_int(stats.get("error_count")),
            "error_codes": error_codes,
            "watermark": _optional_int(stats.get("watermark")),
            "limit_unit": stats.get("limit_unit") or "rows",
            "selected_root_groups": _optional_int(stats.get("selected_root_groups")),
            "returned_root_groups": _optional_int(stats.get("returned_root_groups")),
            "returned_rows": _safe_nonnegative_int(stats.get("returned_rows")),
            "excluded_by_limit": _safe_nonnegative_int(stats.get("excluded_by_limit")),
            "ignored_non_transcript_files": _safe_nonnegative_int(
                stats.get("ignored_non_transcript_files")
            ),
            "unresolved_identity_files": _safe_nonnegative_int(
                stats.get("unresolved_identity_files")
            ),
            "excluded_by_source_namespace": _safe_nonnegative_int(
                stats.get("excluded_by_source_namespace")
            ),
            "unparsed_selected_rows": _safe_nonnegative_int(stats.get("unparsed_selected_rows")),
            "observed_sessions": _safe_nonnegative_int(
                stats.get("observed_sessions")
            ),
            "usage_sessions": _safe_nonnegative_int(stats.get("usage_sessions")),
            "sessions_without_usage": _safe_nonnegative_int(
                stats.get("sessions_without_usage")
            ),
            "source_present": (
                stats.get("source_present")
                if isinstance(stats.get("source_present"), bool)
                else None
            ),
        }
        # Skipped-symlink accounting is specific to the Claude transcript walk
        # (issue #84): the only source that skips-and-continues over an
        # unfollowable directory symlink instead of failing the home closed.
        # Expose it only where it is actually tracked so other sources keep a
        # stable diagnostic shape.
        if "skipped_unsafe_paths" in stats:
            diagnostics[client_name]["skipped_unsafe_paths"] = _safe_nonnegative_int(
                stats.get("skipped_unsafe_paths")
            )
    return ClientUsageDiscoveryResult(
        events=candidates,
        diagnostics=diagnostics,
        session_observations=session_observations,
    )


def discover_client_usage(
    *,
    client: str = "all",
    codex_home: Path | None = None,
    claude_home: Path | None = None,
    opencode_home: Path | None = None,
    hermes_home: Path | None = None,
    openclaw_home: Path | None = None,
    cursor_home: Path | None = None,
    limit_sessions: int = 20,
) -> list[ClientUsageEvent]:
    """Backward-compatible event-only wrapper around diagnostic discovery."""

    return discover_client_usage_with_diagnostics(
        client=client,
        codex_home=codex_home,
        claude_home=claude_home,
        opencode_home=opencode_home,
        hermes_home=hermes_home,
        openclaw_home=openclaw_home,
        cursor_home=cursor_home,
        limit_sessions=limit_sessions,
    ).events


def _client_usage_discovery_error_code(exc: BaseException) -> str:
    if isinstance(exc, _ClientUsageDiscoveryReadError):
        return exc.code
    if isinstance(exc, _ClaudeTranscriptUnsafePathError):
        return "claude_transcript_unsafe_path"
    if isinstance(exc, _ClaudeTranscriptChangedDuringScanError):
        return "claude_transcript_changed_during_scan"
    if isinstance(exc, sqlite3.DatabaseError):
        return "sqlite_read_failed"
    if isinstance(exc, OSError):
        return "filesystem_read_failed"
    return "discovery_failed"


@dataclass(frozen=True)
class LocalUsageImportPlan:
    """Classified import candidates against the stored ledger rows.

    new_candidates: row identity unseen and base session not under migration.
    refresh_candidates: row identity already stored AND its usage CHANGED since
        the stored row; the CLI default path skips these, --refresh and the
        dashboard replace them with fresh totals.
    migration_candidates: the base session has legacy ':model:'-suffixed stored
        rows; those rows (plus any plain stale base-keyed sibling — the
        historical double-count pairing) must be superseded in one
        replace_events transaction with migration provenance.
    replaced_alias_keys_by_base: (client, base session id) -> sorted stored
        client_session_id values that the migration supersedes.
    stored_events_by_identity: row identity -> the stored event it re-observes,
        so a refresh can carry forward that row's migration audit trail.
    stored_events_by_identity_all: every physical row for an identity; alias
        completeness uses this to prevent a later plain shadow from hiding a
        real legacy lane that normalizes to the same identity.
    unchanged_candidates: re-observed identities whose usage is byte-identical to
        the stored row; skipped on EVERY path (even --refresh) so an idle session
        never triggers a ledger rewrite or a reissued event_id/created_at.
    namespace_conflict_candidates: re-observed ids whose persisted source-home
        fingerprint disagrees with this scan; excluded fail-closed.
    stored_source_namespaces_by_base: source namespaces observed for each base
        session, including None for a legacy unscoped row; used by the locked
        replacement predicate to reject stale cross-namespace plans.
    stored_event_ids_by_base: exact row revisions observed at plan time; the
        locked predicate requires them so stale same-namespace plans cannot
        overwrite a newer refresh.
    stored_revisions_by_base: paired event-id/source-namespace revisions for
        atomic alias-cohort guards; a changed sibling aborts the whole write.
    """

    new_candidates: list[ClientUsageEvent]
    refresh_candidates: list[ClientUsageEvent]
    migration_candidates: list[ClientUsageEvent]
    replaced_alias_keys_by_base: dict[tuple[str, str], list[str]]
    stored_events_by_identity: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    stored_events_by_identity_all: dict[
        tuple[str, str, str], tuple[dict[str, Any], ...]
    ] = field(default_factory=dict)
    unchanged_candidates: list[ClientUsageEvent] = field(default_factory=list)
    namespace_conflict_candidates: list[ClientUsageEvent] = field(default_factory=list)
    stored_source_namespaces_by_base: dict[tuple[str, str], frozenset[str | None]] = field(
        default_factory=dict
    )
    stored_event_ids_by_base: dict[tuple[str, str], frozenset[str | None]] = field(
        default_factory=dict
    )
    stored_revisions_by_base: dict[
        tuple[str, str], frozenset[tuple[str | None, str | None]]
    ] = field(default_factory=dict)
    incomplete_migration_bases: frozenset[tuple[str, str]] = frozenset()
    incomplete_source_candidates: list[ClientUsageEvent] = field(default_factory=list)


# Pricing-derived top-level fields: a --refresh WITHOUT --estimate-costs (or the
# dashboard's always-price path) must not force a churn refresh just because the
# stored row was priced. estimated_cost_usd/cost_confidence/cost_basis all flip
# when apply_pricing_estimate_to_event runs, so they are excluded from the
# "did the session's usage actually change?" comparison.
_USAGE_ROW_VALUE_COMPARE_EXCLUDED = frozenset({"estimated_cost_usd", "cost_confidence", "cost_basis"})


def _local_usage_candidate_matches_stored_row(candidate_event: dict[str, Any], stored_event: dict[str, Any]) -> bool:
    """True when a re-observed candidate carries the SAME usage as its stored row.

    Compares every field the importer writes EXCEPT the pricing-derived
    estimated_cost_usd/cost_confidence (carried forward or re-estimated on the
    write path, so they must not force a churn refresh). A True result means the
    session's usage is unchanged, so a --refresh scan can skip it: no ledger
    rewrite and no reissued event_id/created_at on an unchanged row. Extra
    server-owned keys on the stored row (event_id, created_at, usage_provenance,
    trust markers, migration provenance) are ignored — only the candidate's own
    fields are required to match, so the check never false-skips a real change.
    """

    for key, value in candidate_event.items():
        if key in _USAGE_ROW_VALUE_COMPARE_EXCLUDED or key == "metadata":
            continue
        if stored_event.get(key) != value:
            return False
    candidate_md = candidate_event.get("metadata") if isinstance(candidate_event.get("metadata"), dict) else {}
    stored_md = stored_event.get("metadata") if isinstance(stored_event.get("metadata"), dict) else {}
    for key, value in candidate_md.items():
        # Namespace is additive provenance, not usage. A byte-identical legacy
        # row remains unchanged until a real refresh/reprice writes a new row;
        # that first write adopts the namespace with explicit provenance.
        if (
            key in {"source_namespace_fingerprint", "parent_source_namespace_fingerprint"}
            and stored_md.get(key) is None
        ):
            continue
        # The revision watermark is ordering provenance, not usage content.
        # A transcript file's mtime advances whenever the session appends —
        # including turns that leave THIS lane's usage untouched — so
        # comparing it would rewrite unchanged rows on every scan of an
        # active session, which is exactly the churn this gate exists to
        # prevent. Rows adopt the current watermark when real content changes.
        if key in {"source_revision_at", "source_revision_basis"}:
            continue
        if stored_md.get(key) != value:
            return False
    return True


def plan_local_usage_import(
    candidates: list[ClientUsageEvent],
    existing_events: list[dict[str, Any]],
) -> LocalUsageImportPlan:
    """Classify discovered usage candidates as new, refresh, unchanged, or legacy-key migration."""

    existing_identities: set[tuple[str, str, str]] = set()
    stored_events_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    stored_events_by_identity_all: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = {}
    stored_keys_by_base: dict[tuple[str, str], set[str]] = {}
    stored_namespaces_by_base: dict[tuple[str, str], set[str | None]] = {}
    stored_event_ids_by_base: dict[tuple[str, str], set[str | None]] = {}
    stored_revisions_by_base: dict[
        tuple[str, str], set[tuple[str | None, str | None]]
    ] = {}
    suffixed_bases: set[tuple[str, str]] = set()
    for event in existing_events:
        if not (is_local_usage_import_event(event) or is_legacy_local_usage_import_shape(event)):
            continue
        key = local_usage_event_key(event)
        if key is None:
            continue
        client, session_id = key
        base_session = normalized_local_usage_session_id(client, session_id)
        base_key = (client, base_session)
        stored_keys_by_base.setdefault(base_key, set()).add(session_id)
        stored_namespace = _stored_source_namespace_fingerprint(event)
        stored_namespaces_by_base.setdefault(base_key, set()).add(stored_namespace)
        stored_event_id = event.get("event_id")
        stored_event_ids_by_base.setdefault(base_key, set()).add(
            stored_event_id if isinstance(stored_event_id, str) else None
        )
        stored_revisions_by_base.setdefault(base_key, set()).add(
            (
                stored_event_id if isinstance(stored_event_id, str) else None,
                stored_namespace,
            )
        )
        if session_id != base_session:
            suffixed_bases.add(base_key)
        identity = local_usage_row_identity(event)
        if identity is not None:
            existing_identities.add(identity)
            stored_events_by_identity_all.setdefault(identity, []).append(event)
            stored_events_by_identity[identity] = event  # last write wins
    new_candidates: list[ClientUsageEvent] = []
    refresh_candidates: list[ClientUsageEvent] = []
    migration_candidates: list[ClientUsageEvent] = []
    unchanged_candidates: list[ClientUsageEvent] = []
    namespace_conflict_candidates: list[ClientUsageEvent] = []
    incomplete_source_candidates: list[ClientUsageEvent] = []
    replaced_alias_keys_by_base: dict[tuple[str, str], list[str]] = {}
    for candidate in candidates:
        base_key = (candidate.client, normalized_local_usage_session_id(candidate.client, candidate.client_session_id))
        if base_key in suffixed_bases:
            # Retain the candidate base even when the source is quarantined so
            # the planner can report that a destructive alias migration was
            # deliberately preserved rather than silently doing nothing.
            replaced_alias_keys_by_base[base_key] = sorted(stored_keys_by_base[base_key])
        if not candidate.source_parse_complete:
            incomplete_source_candidates.append(candidate)
            continue
        stored_namespaces = stored_namespaces_by_base.get(base_key, set())
        parent_base_key = (
            (candidate.client, normalized_local_usage_session_id(candidate.client, candidate.parent_client_session_id))
            if candidate.parent_client_session_id is not None
            else None
        )
        parent_namespaces = (
            stored_namespaces_by_base.get(parent_base_key, set())
            if parent_base_key is not None
            else set()
        )
        if _source_namespace_set_conflicts(candidate.source_namespace_fingerprint, stored_namespaces) or (
            candidate.parent_client_session_id is not None
            and _source_namespace_set_conflicts(
                candidate.source_namespace_fingerprint,
                parent_namespaces,
            )
        ):
            namespace_conflict_candidates.append(candidate)
        elif base_key in suffixed_bases:
            migration_candidates.append(candidate)
        elif candidate.usage_row_identity in existing_identities:
            stored_event = stored_events_by_identity.get(candidate.usage_row_identity)
            if stored_event is not None and _local_usage_candidate_matches_stored_row(candidate.to_sentinel_event(), stored_event):
                unchanged_candidates.append(candidate)
            else:
                refresh_candidates.append(candidate)
        else:
            new_candidates.append(candidate)
    initial_plan = LocalUsageImportPlan(
        new_candidates=new_candidates,
        refresh_candidates=refresh_candidates,
        migration_candidates=migration_candidates,
        replaced_alias_keys_by_base=replaced_alias_keys_by_base,
        stored_events_by_identity=stored_events_by_identity,
        stored_events_by_identity_all={
            identity: tuple(events)
            for identity, events in stored_events_by_identity_all.items()
        },
        unchanged_candidates=unchanged_candidates,
        namespace_conflict_candidates=namespace_conflict_candidates,
        stored_source_namespaces_by_base={
            base: frozenset(namespaces)
            for base, namespaces in stored_namespaces_by_base.items()
        },
        stored_event_ids_by_base={
            base: frozenset(event_ids)
            for base, event_ids in stored_event_ids_by_base.items()
        },
        stored_revisions_by_base={
            base: frozenset(revisions)
            for base, revisions in stored_revisions_by_base.items()
        },
        incomplete_source_candidates=incomplete_source_candidates,
    )
    return _cohere_legacy_source_namespace_adoptions(
        _quarantine_incomplete_alias_migrations(initial_plan)
    )


def _quarantine_incomplete_alias_migrations(
    plan: LocalUsageImportPlan,
) -> LocalUsageImportPlan:
    """Never delete a legacy base unless every stored lane was re-observed."""

    observed_identities_by_base: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for candidate in plan.migration_candidates:
        observed_identities_by_base.setdefault(_candidate_base_key(candidate), set()).add(
            candidate.usage_row_identity
        )
    incomplete_bases: set[tuple[str, str]] = set()
    for base in plan.replaced_alias_keys_by_base:
        stored_identities = set()
        for identity, representative in plan.stored_events_by_identity.items():
            if identity[:2] != base:
                continue
            physical_rows = plan.stored_events_by_identity_all.get(
                identity,
                (representative,),
            )
            # The historical unsuffixed Claude shadow row had no model/lane,
            # so its synthetic ``model:unknown`` identity can never be
            # re-observed once the transcript exposes real model lanes. Exempt
            # an identity only when EVERY physical row at that identity is
            # exactly that disposable shadow. A real ``:model:unknown`` row
            # sharing the normalized identity must still be re-observed.
            if all(
                _is_unobservable_plain_unknown_alias_shadow(event, base)
                for event in physical_rows
            ):
                continue
            stored_identities.add(identity)
        has_rejected_candidate = any(
            _candidate_base_key(candidate) == base
            for candidate in [
                *plan.namespace_conflict_candidates,
                *plan.incomplete_source_candidates,
            ]
        )
        if has_rejected_candidate or not stored_identities.issubset(
            observed_identities_by_base.get(base, set())
        ):
            incomplete_bases.add(base)
    if not incomplete_bases:
        return plan
    return replace(
        plan,
        migration_candidates=[
            candidate
            for candidate in plan.migration_candidates
            if _candidate_base_key(candidate) not in incomplete_bases
        ],
        replaced_alias_keys_by_base={
            base: keys
            for base, keys in plan.replaced_alias_keys_by_base.items()
            if base not in incomplete_bases
        },
        incomplete_migration_bases=frozenset(incomplete_bases),
    )


def _is_unobservable_plain_unknown_alias_shadow(
    event: dict[str, Any],
    base: tuple[str, str],
) -> bool:
    key = local_usage_event_key(event)
    identity = local_usage_row_identity(event)
    metadata = event.get("metadata")
    if key is None or identity is None or not isinstance(metadata, dict):
        return False
    client, session_id = key
    model = event.get("model")
    return (
        client == "claude-code"
        and session_id == base[1]
        and (model is None or model == "" or model == "unknown")
        and "usage_row_lane" not in metadata
        and identity[2] == USAGE_ROW_LANE_PREFIX + "unknown"
    )


def _candidate_base_key(candidate: ClientUsageEvent) -> tuple[str, str]:
    return (
        candidate.client,
        normalized_local_usage_session_id(candidate.client, candidate.client_session_id),
    )


def bind_discovered_usage_source_namespaces(
    service: Any,
    candidates: list[ClientUsageEvent],
    *,
    write: bool = True,
) -> tuple[
    list[ClientUsageEvent],
    list[ClientUsageEvent],
    list[ClientUsageEvent],
    dict[str, int],
]:
    """TOFU-bind explicitly scanned usage rows before planning an import.

    Returns ``(accepted, adopted, conflicts, adopted_rows_by_client)``. The service performs one
    locked metadata-only rewrite for every legacy row in the scan while
    preserving event ids and timestamps. A dry run passes ``write=False`` and
    receives the same decision without mutating the ledger.
    """

    candidates_by_base: dict[tuple[str, str], list[ClientUsageEvent]] = {}
    for candidate in candidates:
        candidates_by_base.setdefault(_candidate_base_key(candidate), []).append(candidate)
    bindings: dict[tuple[str, str], str] = {}
    preflight_conflicts: set[tuple[str, str]] = set()
    for base, base_candidates in candidates_by_base.items():
        namespaces = {
            candidate.source_namespace_fingerprint
            for candidate in base_candidates
            if candidate.source_namespace_fingerprint is not None
        }
        missing = any(
            candidate.source_namespace_fingerprint is None
            for candidate in base_candidates
        )
        if len(namespaces) > 1 or (namespaces and missing):
            preflight_conflicts.add(base)
        elif len(namespaces) == 1:
            bindings[base] = next(iter(namespaces))

    outcome = service.bind_local_usage_source_namespaces(bindings, write=write)
    conflict_bases = preflight_conflicts | set(outcome.get("conflict_bases") or set())
    bound_identities = set(outcome.get("bound_identities") or set())
    accepted = [
        candidate
        for candidate in candidates
        if _candidate_base_key(candidate) not in conflict_bases
    ]
    adopted = [
        candidate
        for candidate in accepted
        if candidate.usage_row_identity in bound_identities
    ]
    conflicts = [
        candidate
        for candidate in candidates
        if _candidate_base_key(candidate) in conflict_bases
    ]
    adopted_rows_by_client = {
        str(client): _safe_nonnegative_int(count)
        for client, count in dict(outcome.get("bound_rows_by_client") or {}).items()
        if _safe_nonnegative_int(count)
    }
    return accepted, adopted, conflicts, adopted_rows_by_client


def _cohere_legacy_source_namespace_adoptions(
    plan: LocalUsageImportPlan,
) -> LocalUsageImportPlan:
    """Adopt a source namespace for every stored lane, or for none.

    A real write (changed, repriced, migrated, or newly observed lane) may
    introduce explicit source provenance into a legacy unscoped session. Every
    stored lane must be re-observed in that same home so unchanged siblings can
    be promoted into the same atomic refresh. If coverage is incomplete, all
    writes for that base fail closed instead of creating a mixed-scope cohort.
    Ordinary all-unchanged scans remain no-ops.
    """

    new_candidates = list(plan.new_candidates)
    refresh_candidates = list(plan.refresh_candidates)
    migration_candidates = list(plan.migration_candidates)
    unchanged_candidates = list(plan.unchanged_candidates)
    namespace_conflicts = list(plan.namespace_conflict_candidates)
    write_candidates = [*new_candidates, *refresh_candidates, *migration_candidates]
    triggered_bases = {
        _candidate_base_key(candidate)
        for candidate in write_candidates
        if candidate.source_namespace_fingerprint is not None
        and plan.stored_source_namespaces_by_base.get(_candidate_base_key(candidate))
        == frozenset({None})
    }
    for base in triggered_bases:
        base_candidates = [
            candidate
            for candidate in [
                *new_candidates,
                *refresh_candidates,
                *migration_candidates,
                *unchanged_candidates,
            ]
            if _candidate_base_key(candidate) == base
        ]
        namespaces = {
            candidate.source_namespace_fingerprint
            for candidate in base_candidates
            if candidate.source_namespace_fingerprint is not None
        }
        stored_identities = {
            identity
            for identity in plan.stored_events_by_identity
            if identity[:2] == base
        }
        observed_identities = {candidate.usage_row_identity for candidate in base_candidates}
        base_writes = [candidate for candidate in write_candidates if _candidate_base_key(candidate) == base]
        # A legacy ``:model:`` migration deliberately supersedes the complete
        # alias cohort, including the historical unsuffixed shadow row whose
        # synthetic ``model:unknown`` identity cannot be re-observed.  Its
        # whole-base revision guard provides the atomicity guarantee.  Normal
        # legacy adoption still requires every stored lane to be re-observed.
        has_complete_alias_migration = base in plan.replaced_alias_keys_by_base
        has_complete_lane_coverage = stored_identities.issubset(observed_identities)
        if len(namespaces) != 1 or not (
            has_complete_alias_migration or has_complete_lane_coverage
        ):
            new_candidates = [candidate for candidate in new_candidates if candidate not in base_writes]
            refresh_candidates = [candidate for candidate in refresh_candidates if candidate not in base_writes]
            migration_candidates = [candidate for candidate in migration_candidates if candidate not in base_writes]
            for candidate in base_writes:
                if candidate not in namespace_conflicts:
                    namespace_conflicts.append(candidate)
            continue
        target_namespace = next(iter(namespaces))
        companions = [
            candidate
            for candidate in unchanged_candidates
            if _candidate_base_key(candidate) == base
            and candidate.usage_row_identity in stored_identities
            and candidate.source_namespace_fingerprint == target_namespace
        ]
        refresh_candidates.extend(
            candidate for candidate in companions if candidate not in refresh_candidates
        )
        unchanged_candidates = [
            candidate for candidate in unchanged_candidates if candidate not in companions
        ]
    return replace(
        plan,
        new_candidates=new_candidates,
        refresh_candidates=refresh_candidates,
        migration_candidates=migration_candidates,
        unchanged_candidates=unchanged_candidates,
        namespace_conflict_candidates=namespace_conflicts,
    )


def _source_namespace_set_conflicts(
    candidate_namespace: str | None,
    stored_namespaces: set[str | None],
) -> bool:
    if candidate_namespace is None or not stored_namespaces:
        return False
    scoped = {namespace for namespace in stored_namespaces if namespace is not None}
    if not scoped:
        return False
    return len(stored_namespaces) > 1 or candidate_namespace not in scoped


def _stored_source_namespace_fingerprint(event: dict[str, Any]) -> str | None:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("source_namespace_fingerprint")
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return None
    return value


def usage_rows_replace_predicate(
    *,
    identities: set[tuple[str, str, str]],
    alias_bases: set[tuple[str, str]],
    expected_namespaces_by_identity: Mapping[tuple[str, str, str], str | None] | None = None,
    expected_namespaces_by_base: Mapping[tuple[str, str], frozenset[str | None]] | None = None,
    expected_event_ids_by_identity: Mapping[tuple[str, str, str], str | None] | None = None,
    expected_event_ids_by_base: Mapping[tuple[str, str], frozenset[str | None]] | None = None,
) -> Callable[[dict[str, Any]], bool]:
    """Predicate for service.replace_events: replace exactly the re-observed rows.

    A stored event is replaced iff it is a local usage import row AND either its
    (client, base session id) is a base under legacy-key migration or its
    (client, base session id, lane) identity was re-derived this scan. Notes,
    MCP events, and never-re-observed import rows are always kept.
    """

    def _should_replace(event: dict[str, Any]) -> bool:
        if not (is_local_usage_import_event(event) or is_legacy_local_usage_import_shape(event)):
            return False
        key = local_usage_event_key(event)
        if key is None:
            return False
        client, session_id = key
        base = (client, normalized_local_usage_session_id(client, session_id))
        current_namespace = _stored_source_namespace_fingerprint(event)
        current_event_id = event.get("event_id") if isinstance(event.get("event_id"), str) else None
        if base in alias_bases:
            if expected_namespaces_by_base is not None and base in expected_namespaces_by_base:
                if current_namespace not in expected_namespaces_by_base[base]:
                    return False
            if expected_event_ids_by_base is not None and base in expected_event_ids_by_base:
                return current_event_id in expected_event_ids_by_base[base]
            return True
        identity = local_usage_row_identity(event)
        if identity not in identities:
            return False
        if expected_namespaces_by_identity is not None and identity in expected_namespaces_by_identity:
            if current_namespace != expected_namespaces_by_identity[identity]:
                return False
        if expected_event_ids_by_identity is not None and identity in expected_event_ids_by_identity:
            return current_event_id == expected_event_ids_by_identity[identity]
        return True

    return _should_replace


def usage_rows_replace_guard(
    plan: LocalUsageImportPlan,
    import_candidates: list[ClientUsageEvent],
    *,
    expected_unchanged_bases: set[tuple[str, str]] | None = None,
) -> Callable[[list[dict[str, Any]]], bool]:
    """Build exact all-or-nothing preconditions evaluated under the write lock.

    The guard is scoped to the candidates in this write, not every candidate in
    the scan.  Refresh/migration rows must retain the revisions observed while
    planning, and NEW identities must still be absent.  The latter prevents a
    concurrent import from another source home from creating a mixed-source
    multi-lane session while legacy sibling lanes are atomically adopting a
    namespace.
    """

    import_identities = {candidate.usage_row_identity for candidate in import_candidates}
    planned_new_identities = {
        candidate.usage_row_identity for candidate in plan.new_candidates
    }
    expected_absent_identities = import_identities & planned_new_identities

    expected_by_identity: dict[
        tuple[str, str, str], tuple[str | None, str | None]
    ] = {}
    for candidate in plan.refresh_candidates:
        if candidate.usage_row_identity not in import_identities:
            continue
        stored = plan.stored_events_by_identity.get(candidate.usage_row_identity)
        if stored is None:
            continue
        event_id = stored.get("event_id")
        expected_by_identity[candidate.usage_row_identity] = (
            event_id if isinstance(event_id, str) else None,
            _stored_source_namespace_fingerprint(stored),
        )
    actual_alias_bases = {
        (
            candidate.client,
            normalized_local_usage_session_id(candidate.client, candidate.client_session_id),
        )
        for candidate in import_candidates
        if (
            candidate.client,
            normalized_local_usage_session_id(candidate.client, candidate.client_session_id),
        )
        in plan.replaced_alias_keys_by_base
    }
    whole_base_guards = actual_alias_bases | set(expected_unchanged_bases or set())
    expected_by_base = {
        base: plan.stored_revisions_by_base.get(base, frozenset())
        for base in whole_base_guards
    }

    def _guard(existing: list[dict[str, Any]]) -> bool:
        usage_rows = [
            event
            for event in existing
            if recognized_local_usage_row_identity(event) is not None
        ]
        current_identities = {
            identity
            for event in usage_rows
            if (identity := recognized_local_usage_row_identity(event)) is not None
        }
        if current_identities & expected_absent_identities:
            return False
        for identity, expected_revision in expected_by_identity.items():
            current = [
                event
                for event in usage_rows
                if recognized_local_usage_row_identity(event) == identity
            ]
            if len(current) != 1:
                return False
            event_id = current[0].get("event_id")
            current_revision = (
                event_id if isinstance(event_id, str) else None,
                _stored_source_namespace_fingerprint(current[0]),
            )
            if current_revision != expected_revision:
                return False
        for base, expected_revisions in expected_by_base.items():
            current_revisions: set[tuple[str | None, str | None]] = set()
            for event in usage_rows:
                key = local_usage_event_key(event)
                if key is None:
                    continue
                client, session_id = key
                if (client, normalized_local_usage_session_id(client, session_id)) != base:
                    continue
                event_id = event.get("event_id")
                current_revisions.add(
                    (
                        event_id if isinstance(event_id, str) else None,
                        _stored_source_namespace_fingerprint(event),
                    )
                )
            if frozenset(current_revisions) != expected_revisions:
                return False
        return True

    return _guard


def annotate_migrated_usage_event(event: dict[str, Any], superseded_keys: list[str]) -> None:
    """Stamp migration provenance on a replacement usage event.

    The superseded legacy client_session_id values stay auditable on the fresh
    row: legacy rows are superseded inside one replace_events transaction,
    never silently dropped.
    """

    metadata = event.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["migrated_from_client_session_ids"] = list(superseded_keys)
        metadata["usage_key_migration_reason"] = USAGE_KEY_MIGRATION_REASON


def _carry_forward_migration_provenance(event: dict[str, Any], stored_event: dict[str, Any]) -> None:
    """Carry a replaced row's migration audit trail onto its fresh replacement.

    A refresh that supersedes a previously-migrated row must keep
    migrated_from_client_session_ids + usage_key_migration_reason so the
    superseded legacy keys stay auditable (the annotate_migrated_usage_event
    contract: 'never silently dropped'), instead of losing them on the rebuilt
    row. The fresh row's own annotation, if any, wins.
    """

    stored_md = stored_event.get("metadata")
    if not isinstance(stored_md, dict):
        return
    migrated = stored_md.get("migrated_from_client_session_ids")
    if not migrated:
        return
    event_md = event.setdefault("metadata", {})
    if isinstance(event_md, dict) and not event_md.get("migrated_from_client_session_ids"):
        event_md["migrated_from_client_session_ids"] = list(migrated) if isinstance(migrated, list) else migrated
        reason = stored_md.get("usage_key_migration_reason")
        if reason is not None:
            event_md["usage_key_migration_reason"] = reason


def _carry_forward_source_namespace_provenance(
    event: dict[str, Any],
    stored_events: list[dict[str, Any]],
) -> None:
    """Keep the audit reason when a TOFU-bound row is later rebuilt."""

    values_by_key: dict[str, set[str]] = {
        "source_namespace_binding": set(),
        "source_namespace_adoption": set(),
    }
    for stored_event in stored_events:
        stored_md = stored_event.get("metadata")
        if not isinstance(stored_md, dict):
            continue
        for key in values_by_key:
            value = stored_md.get(key)
            if isinstance(value, str) and value:
                values_by_key[key].add(value)
    event_md = event.setdefault("metadata", {})
    if not isinstance(event_md, dict):
        return
    for key, values in values_by_key.items():
        if len(values) == 1 and not event_md.get(key):
            event_md[key] = next(iter(values))


def _candidate_adopts_source_namespace(
    candidate: ClientUsageEvent,
    plan: LocalUsageImportPlan,
) -> bool:
    if candidate.source_namespace_fingerprint is None:
        return False
    stored = plan.stored_events_by_identity.get(candidate.usage_row_identity)
    return stored is not None and _stored_source_namespace_fingerprint(stored) is None


def source_namespace_adoption_candidates(
    import_candidates: list[ClientUsageEvent],
    plan: LocalUsageImportPlan,
) -> list[ClientUsageEvent]:
    refresh_identities = {candidate.usage_row_identity for candidate in plan.refresh_candidates}
    return [
        candidate
        for candidate in import_candidates
        if candidate.usage_row_identity in refresh_identities
        and _candidate_adopts_source_namespace(candidate, plan)
    ]


def select_usage_import_candidates(
    plan: LocalUsageImportPlan,
    *,
    include_refresh: bool,
) -> list[ClientUsageEvent]:
    """Select a write set without splitting a namespace-adoption cohort.

    The default CLI policy still skips ordinary refreshes.  When a genuinely
    NEW lane would introduce source-home provenance into a legacy unscoped
    multi-lane session, however, the unchanged sibling lanes promoted by
    ``_cohere_legacy_source_namespace_adoptions`` are mandatory companions.
    Writing only the new lane would create a mixed-scope cohort that read-time
    correctly quarantines.  Include those companions even without
    ``--refresh``; a refresh-only scan remains a no-op under the default.
    """

    if include_refresh:
        return [
            *plan.new_candidates,
            *plan.refresh_candidates,
            *plan.migration_candidates,
        ]
    default_candidates = [*plan.new_candidates, *plan.migration_candidates]
    adoption_trigger_bases = {
        _candidate_base_key(candidate)
        for candidate in default_candidates
        if candidate.source_namespace_fingerprint is not None
        and plan.stored_source_namespaces_by_base.get(_candidate_base_key(candidate))
        == frozenset({None})
    }
    mandatory_adoptions = [
        candidate
        for candidate in plan.refresh_candidates
        if _candidate_base_key(candidate) in adoption_trigger_bases
        and _candidate_adopts_source_namespace(candidate, plan)
    ]
    return [*plan.new_candidates, *mandatory_adoptions, *plan.migration_candidates]


def classify_usage_write_conflict_candidates(
    import_candidates: list[ClientUsageEvent],
    plan: LocalUsageImportPlan,
    recorded_events: list[dict[str, Any]],
    current_events: list[dict[str, Any]],
) -> tuple[list[ClientUsageEvent], list[ClientUsageEvent]]:
    replacement_identities = {
        candidate.usage_row_identity
        for candidate in [*plan.refresh_candidates, *plan.migration_candidates]
    }
    recorded_identities = {
        identity
        for event in recorded_events
        if (identity := recognized_local_usage_row_identity(event)) is not None
    }
    missing = [
        candidate
        for candidate in import_candidates
        if candidate.usage_row_identity not in recorded_identities
    ]
    current_by_identity = {
        identity: event
        for event in current_events
        if (identity := recognized_local_usage_row_identity(event)) is not None
    }
    current_by_base: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in current_events:
        identity = recognized_local_usage_row_identity(event)
        if identity is not None:
            current_by_base.setdefault(identity[:2], []).append(event)
    namespace_conflicts: list[ClientUsageEvent] = []
    concurrent_refresh_conflicts: list[ClientUsageEvent] = []
    for candidate in missing:
        current = current_by_identity.get(candidate.usage_row_identity)
        current_base_rows = current_by_base.get(_candidate_base_key(candidate), [])
        current_namespace = (
            _stored_source_namespace_fingerprint(current)
            if current is not None
            else None
        )
        if current is not None and candidate.source_namespace_fingerprint != current_namespace and (
            candidate.source_namespace_fingerprint is not None or current_namespace is not None
        ):
            namespace_conflicts.append(candidate)
        elif current is None and current_base_rows:
            current_base_namespaces = {
                _stored_source_namespace_fingerprint(event)
                for event in current_base_rows
            }
            if (
                len(current_base_namespaces) > 1
                or candidate.source_namespace_fingerprint not in current_base_namespaces
            ):
                namespace_conflicts.append(candidate)
            else:
                # A same-source lane appeared after planning and the bulk
                # expected-empty base dedup skipped this candidate.  A retry
                # can safely replan it as a new lane joining an existing base.
                concurrent_refresh_conflicts.append(candidate)
        elif candidate.usage_row_identity in replacement_identities:
            concurrent_refresh_conflicts.append(candidate)
    return namespace_conflicts, concurrent_refresh_conflicts


def build_usage_import_diagnostics(
    diagnostics: Mapping[str, Mapping[str, Any]],
    *,
    namespace_conflict_candidates: list[ClientUsageEvent],
    namespace_adoption_candidates: list[ClientUsageEvent],
    namespace_adoption_counts: Mapping[str, int] | None = None,
    concurrent_refresh_conflict_candidates: list[ClientUsageEvent] | None = None,
    incomplete_migration_bases: frozenset[tuple[str, str]] = frozenset(),
) -> dict[str, dict[str, Any]]:
    conflict_counts: dict[str, int] = {}
    adoption_counts: dict[str, int] = {
        str(client): _safe_nonnegative_int(count)
        for client, count in (namespace_adoption_counts or {}).items()
        if _safe_nonnegative_int(count)
    }
    concurrent_counts: dict[str, int] = {}
    incomplete_counts: dict[str, int] = {}
    for candidate in namespace_conflict_candidates:
        conflict_counts[candidate.client] = conflict_counts.get(candidate.client, 0) + 1
    for candidate in namespace_adoption_candidates:
        adoption_counts[candidate.client] = adoption_counts.get(candidate.client, 0) + 1
    for candidate in concurrent_refresh_conflict_candidates or []:
        concurrent_counts[candidate.client] = concurrent_counts.get(candidate.client, 0) + 1
    for client, _session_id in incomplete_migration_bases:
        incomplete_counts[client] = incomplete_counts.get(client, 0) + 1
    merged: dict[str, dict[str, Any]] = {}
    for client, diagnostic in diagnostics.items():
        row = dict(diagnostic)
        row["source_namespace_conflicts"] = (
            _safe_nonnegative_int(row.get("excluded_by_source_namespace"))
            + conflict_counts.get(client, 0)
        )
        row["source_namespace_adoptions"] = adoption_counts.get(client, 0)
        row["concurrent_refresh_conflicts"] = concurrent_counts.get(client, 0)
        incomplete_count = incomplete_counts.get(client, 0)
        row["incomplete_alias_migrations"] = incomplete_count
        if incomplete_count:
            row["error_count"] = _safe_nonnegative_int(row.get("error_count")) + incomplete_count
            error_codes = [
                str(code)
                for code in (row.get("error_codes") or [])
                if isinstance(code, str)
            ]
            if "alias_migration_incomplete" not in error_codes:
                error_codes.append("alias_migration_incomplete")
            row["error_codes"] = error_codes
        merged[client] = row
    return merged


def build_usage_import_write(
    import_candidates: list[ClientUsageEvent],
    plan: LocalUsageImportPlan,
    *,
    expected_unchanged_bases: set[tuple[str, str]] | None = None,
) -> tuple[
    list[dict[str, Any]],
    Callable[[dict[str, Any]], bool],
    Callable[[list[dict[str, Any]]], bool],
]:
    """Build the replacement events plus matching predicate for one import write.

    Shared by the CLI/watch payload and the dashboard button so every importer
    uses identical identity/replace semantics. The caller decides WHICH
    candidates to write (the CLI default excludes refresh candidates; the
    dashboard and ``--refresh`` include them).

    The replace predicate is scoped to legacy-key migration bases plus the
    identities of REFRESH candidates only — never NEW candidates. A new row must
    never REPLACE an existing row (the never-refresh default): it is inserted
    only if its identity is absent at write time (``service.replace_events``'s
    ``dedup_key``), so a stale scan can neither duplicate a concurrent import nor
    revert a fresher refresh. A refresh candidate that supersedes a
    previously-migrated row carries that row's migration provenance forward, so
    the superseded legacy keys stay auditable.
    """

    refresh_identities = {candidate.usage_row_identity for candidate in plan.refresh_candidates}
    actual_alias_bases = {
        (
            candidate.client,
            normalized_local_usage_session_id(candidate.client, candidate.client_session_id),
        )
        for candidate in import_candidates
        if (
            candidate.client,
            normalized_local_usage_session_id(candidate.client, candidate.client_session_id),
        )
        in plan.replaced_alias_keys_by_base
    }
    events: list[dict[str, Any]] = []
    predicate_identities: set[tuple[str, str, str]] = set()
    expected_namespaces_by_identity: dict[tuple[str, str, str], str | None] = {}
    expected_event_ids_by_identity: dict[tuple[str, str, str], str | None] = {}
    for candidate in import_candidates:
        event = candidate.to_sentinel_event()
        candidate_base = (
            candidate.client,
            normalized_local_usage_session_id(candidate.client, candidate.client_session_id),
        )
        alias_keys = plan.replaced_alias_keys_by_base.get(candidate_base)
        if alias_keys is not None:
            annotate_migrated_usage_event(event, alias_keys)
            _carry_forward_source_namespace_provenance(
                event,
                [
                    stored
                    for identity, stored in plan.stored_events_by_identity.items()
                    if identity[:2] == candidate_base
                ],
            )
        elif candidate.usage_row_identity in refresh_identities:
            stored_event = plan.stored_events_by_identity.get(candidate.usage_row_identity)
            if stored_event is not None:
                _carry_forward_migration_provenance(event, stored_event)
                _carry_forward_source_namespace_provenance(event, [stored_event])
                expected_namespaces_by_identity[candidate.usage_row_identity] = (
                    _stored_source_namespace_fingerprint(stored_event)
                )
                stored_event_id = stored_event.get("event_id")
                expected_event_ids_by_identity[candidate.usage_row_identity] = (
                    stored_event_id if isinstance(stored_event_id, str) else None
                )
                if _candidate_adopts_source_namespace(candidate, plan):
                    metadata = event.setdefault("metadata", {})
                    if isinstance(metadata, dict):
                        metadata["source_namespace_adoption"] = "legacy_unscoped_on_refresh"
            predicate_identities.add(candidate.usage_row_identity)
        events.append(event)
    predicate = usage_rows_replace_predicate(
        identities=predicate_identities,
        alias_bases=actual_alias_bases,
        expected_namespaces_by_identity=expected_namespaces_by_identity,
        expected_namespaces_by_base={
            base: plan.stored_source_namespaces_by_base.get(base, frozenset())
            for base in actual_alias_bases
        },
        expected_event_ids_by_identity=expected_event_ids_by_identity,
        expected_event_ids_by_base={
            base: plan.stored_event_ids_by_base.get(base, frozenset())
            for base in actual_alias_bases
        },
    )
    return events, predicate, usage_rows_replace_guard(
        plan,
        import_candidates,
        expected_unchanged_bases=expected_unchanged_bases,
    )


def build_usage_import_write_batches(
    import_candidates: list[ClientUsageEvent],
    plan: LocalUsageImportPlan,
) -> list[
    tuple[
        list[dict[str, Any]],
        Callable[[dict[str, Any]], bool],
        Callable[[list[dict[str, Any]]], bool] | None,
        Callable[[dict[str, Any]], Any],
    ]
]:
    """Build one common write plus rare atomic client-session cohorts.

    Pure NEW rows and ordinary explicit-namespace refreshes are safe to combine
    in one unguarded write.  Refreshes use row-identity dedup.  Bases expected
    to be entirely new use base-session dedup, so ANY lane that raced into that
    base skips the whole candidate cohort while unrelated bases still land.
    This keeps a 500-session dashboard scan to one full-ledger rewrite instead
    of 500 rewrites/fsyncs without letting two stale Claude lane plans mix homes.

    Legacy alias migrations, source-namespace adoption cohorts, and a new lane
    joining an existing session still need all-or-nothing, per-base revision
    guards.  They remain separate (and rare) so a race cannot partially migrate
    a lane cohort or mix source homes. Dict insertion order preserves order.
    """

    candidates_by_base: dict[tuple[str, str], list[ClientUsageEvent]] = {}
    for candidate in import_candidates:
        candidates_by_base.setdefault(_candidate_base_key(candidate), []).append(candidate)
    planned_new_identities = {
        candidate.usage_row_identity for candidate in plan.new_candidates
    }
    atomic_bases: set[tuple[str, str]] = set()
    for base, candidates in candidates_by_base.items():
        has_new = any(
            candidate.usage_row_identity in planned_new_identities
            for candidate in candidates
        )
        new_row_needs_cohort_guard = has_new and base in plan.stored_revisions_by_base
        if (
            base in plan.replaced_alias_keys_by_base
            or any(_candidate_adopts_source_namespace(candidate, plan) for candidate in candidates)
            or new_row_needs_cohort_guard
        ):
            atomic_bases.add(base)
    common_candidates = [
        candidate
        for candidate in import_candidates
        if _candidate_base_key(candidate) not in atomic_bases
    ]
    batches: list[
        tuple[
            list[dict[str, Any]],
            Callable[[dict[str, Any]], bool],
            Callable[[list[dict[str, Any]]], bool] | None,
            Callable[[dict[str, Any]], Any],
        ]
    ] = []
    if common_candidates:
        events, predicate, _unused_guard = build_usage_import_write(common_candidates, plan)
        expected_empty_bases = {
            _candidate_base_key(candidate)
            for candidate in common_candidates
            if candidate.usage_row_identity in planned_new_identities
            and _candidate_base_key(candidate) not in plan.stored_revisions_by_base
        }
        batches.append(
            (
                events,
                predicate,
                None,
                _usage_import_batch_dedup_key(expected_empty_bases),
            )
        )
    for base, candidates in candidates_by_base.items():
        if base in atomic_bases:
            events, predicate, guard = build_usage_import_write(
                candidates,
                plan,
                expected_unchanged_bases={base},
            )
            batches.append((events, predicate, guard, recognized_local_usage_row_identity))
    return batches


def _usage_import_batch_dedup_key(
    expected_empty_bases: set[tuple[str, str]],
) -> Callable[[dict[str, Any]], Any]:
    """Dedup rows, but condition all expected-new lanes on an empty base."""

    def _key(event: dict[str, Any]) -> Any:
        identity = recognized_local_usage_row_identity(event)
        if identity is None:
            return None
        base = identity[:2]
        if base in expected_empty_bases:
            return ("base", *base)
        return ("row", *identity)

    return _key


def promote_unknown_cost_reprices(plan: LocalUsageImportPlan) -> tuple[LocalUsageImportPlan, list[ClientUsageEvent]]:
    """Promote unknown-cost unchanged rows to refresh-worthy when they NOW price.

    The unknown→priced transition ONLY: an unchanged re-observed row whose
    stored ``cost_confidence`` is unknown/absent AND whose (provider, model)
    now resolves in the active pricing catalog is moved into the refresh set,
    so the write path replaces it with a priced row (``pricing_source``
    provenance stamped, event id reissued once — after which the row is priced
    and never qualifies again). Already-priced rows keep the Phase-3 stability
    rule: pricing-value drift in the catalog never rewrites a stored row, and
    ``client_reported`` costs are never overwritten (the same guard shape as
    ``apply_pricing_estimate_to_event``).

    Callers gate this on their pricing paths: the CLI/watch importer only
    under ``--refresh`` with ``--estimate-costs``; the dashboard refresh
    always (it always prices).
    """

    repriced: list[ClientUsageEvent] = []
    for candidate in plan.unchanged_candidates:
        stored = plan.stored_events_by_identity.get(candidate.usage_row_identity)
        if stored is None:
            continue
        confidence = stored.get("cost_confidence")
        if confidence is not None and str(confidence) not in ("", COST_UNKNOWN):
            continue
        provider = str(stored.get("provider") or "")
        model = str(stored.get("model") or "")
        if not provider or not model or not has_model_price(provider, model):
            continue
        repriced.append(candidate)
    if not repriced:
        return plan, []
    repriced_identities = {candidate.usage_row_identity for candidate in repriced}
    promoted = replace(
        plan,
        refresh_candidates=[*plan.refresh_candidates, *repriced],
        unchanged_candidates=[
            candidate
            for candidate in plan.unchanged_candidates
            if candidate.usage_row_identity not in repriced_identities
        ],
    )
    return (
        _cohere_legacy_source_namespace_adoptions(promoted),
        repriced,
    )


def apply_pricing_estimate_to_event(event: dict[str, Any]) -> bool:
    """Attach a local list-price estimate to a client usage event when known."""

    if event.get("estimated_cost_usd") is not None and event.get("cost_confidence") == COST_CLIENT_REPORTED:
        return False
    metadata = event.get("metadata")
    if isinstance(metadata, dict) and metadata.get("usage_additive") is False:
        # The raw cumulative row remains available for forensic inspection,
        # but pricing it would turn an explicitly non-additive observation
        # into a misleading dollar claim.
        return False
    provider = str(event.get("provider") or "")
    model = str(event.get("model") or "")
    if not provider or not model or not has_model_price(provider, model):
        return False
    metadata = event.setdefault("metadata", {})
    cache_creation_input_tokens = _safe_nonnegative_int(metadata.get("cache_creation_input_tokens") if isinstance(metadata, dict) else 0)
    cache_read_input_tokens = _safe_nonnegative_int(metadata.get("cache_read_input_tokens") if isinstance(metadata, dict) else 0)
    cached_input_tokens = _safe_nonnegative_int(metadata.get("cached_input_tokens") if isinstance(metadata, dict) else 0)
    if cache_creation_input_tokens + cache_read_input_tokens <= 0:
        cache_read_input_tokens = cached_input_tokens
    input_tokens = _safe_nonnegative_int(event.get("estimated_input_tokens"))
    output_tokens = _safe_nonnegative_int(event.get("estimated_output_tokens"))
    breakdown = estimate_model_cost_breakdown_usd(
        provider,
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_5m_input_tokens=_safe_nonnegative_int(metadata.get("cache_creation_5m_input_tokens") if isinstance(metadata, dict) else 0),
        cache_creation_1h_input_tokens=_safe_nonnegative_int(metadata.get("cache_creation_1h_input_tokens") if isinstance(metadata, dict) else 0),
    )
    event["estimated_cost_usd"] = round(breakdown["total_cost_usd"], 12)
    event["cost_confidence"] = COST_ESTIMATED_FROM_TOKENS
    event["cost_basis"] = COST_BASIS_PRICING_TABLE
    if isinstance(metadata, dict):
        pricing_entry = model_pricing_entry(provider, model)
        # frozen fallback value: "pricing_source" is stored vocabulary (pre-rename).
        metadata["pricing_source"] = pricing_entry.source if pricing_entry is not None else "agent_sentinel_pricing_catalog"
        if pricing_entry is not None:
            metadata["pricing_source_model"] = pricing_entry.source_model or pricing_entry.model
            metadata["pricing_source_provider"] = pricing_entry.source_provider or pricing_entry.provider
        metadata["pricing_warning"] = "estimated equivalent cost, not provider invoice or subscription billing"
    return True


def _discover_codex_rollout_only_rows(
    codex_home: Path,
    *,
    indexed_rows: list[dict[str, Any]],
    _discovery_stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Discover authoritative rollout sessions missing from Codex's sqlite index.

    A rollout contributes only when its own ``session_meta`` carries an exact
    session id. Filenames, cwd, and timestamps are never used to invent an
    identity. Symlinks and non-regular files are never imported; diagnostics
    mark them unresolved so a full-history rebuild cannot silently omit a
    rollout-shaped source while claiming completeness.
    """

    def record_inventory_issue(code: str, *, unresolved_identity: bool) -> None:
        if _discovery_stats is None:
            return
        _discovery_stats["error_count"] = _safe_nonnegative_int(
            _discovery_stats.get("error_count")
        ) + 1
        if unresolved_identity:
            _discovery_stats["unresolved_identity_files"] = (
                _safe_nonnegative_int(
                    _discovery_stats.get("unresolved_identity_files")
                )
                + 1
            )
        error_codes = [
            item
            for item in _discovery_stats.get("error_codes") or []
            if isinstance(item, str)
        ]
        if code not in error_codes:
            error_codes.append(code)
        _discovery_stats["error_codes"] = error_codes

    indexed_paths: set[str] = set()
    for row in indexed_rows:
        configured_path = row.get("rollout_path")
        if not configured_path:
            continue
        indexed_source = _codex_rollout_source(
            Path(str(configured_path)),
            codex_root=codex_home,
        )
        if indexed_source is not None:
            indexed_paths.add(_canonical_source_file_path(indexed_source.path))
    indexed_session_ids = {
        _codex_row_session_id(row)
        for row in indexed_rows
        if _codex_row_session_id(row)
    }
    rollout_only_by_session: dict[str, dict[str, Any]] = {}
    for transcript_root in (
        codex_home / "sessions",
        codex_home / "archived_sessions",
    ):
        try:
            transcript_root_stat = transcript_root.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            record_inventory_issue(
                "codex_rollout_inventory_failed",
                unresolved_identity=False,
            )
            continue
        if not stat.S_ISDIR(transcript_root_stat.st_mode):
            record_inventory_issue(
                "codex_rollout_inventory_failed",
                unresolved_identity=False,
            )
            continue
        canonical_transcript_root = _canonical_source_file_path(transcript_root)
        transcript_root_fd: int | None = None
        try:
            transcript_root_fd, _root_stat = _open_directory_root_fd_no_follow(
                transcript_root
            )
            candidates = _discover_source_tree_files_no_follow(
                transcript_root,
                root_fd=transcript_root_fd,
                include_file=lambda name: name.startswith("rollout-")
                and name.endswith(".jsonl"),
                unsafe_code="codex_rollout_inventory_failed",
                traversal_code="codex_rollout_inventory_failed",
                changed_code="codex_rollout_inventory_changed",
            )
            for candidate in candidates:
                path = candidate.path
                if candidate.fingerprint is None:
                    record_inventory_issue(
                        "codex_rollout_identity_unresolved",
                        unresolved_identity=True,
                    )
                    continue
                source = _regular_source_file(path, root=transcript_root)
                if source is None:
                    record_inventory_issue(
                        "codex_rollout_identity_unresolved",
                        unresolved_identity=True,
                    )
                    continue
                if (
                    source.device,
                    source.inode,
                    source.size,
                    source.mtime_ns,
                ) != (
                    candidate.fingerprint[0],
                    candidate.fingerprint[1],
                    candidate.fingerprint[2],
                    candidate.fingerprint[3],
                ):
                    record_inventory_issue(
                        "codex_rollout_inventory_changed",
                        unresolved_identity=True,
                    )
                    continue
                canonical_path = _canonical_source_file_path(source.path)
                try:
                    if os.path.commonpath(
                        [canonical_transcript_root, canonical_path]
                    ) != canonical_transcript_root:
                        record_inventory_issue(
                            "codex_rollout_identity_unresolved",
                            unresolved_identity=True,
                        )
                        continue
                except ValueError:
                    record_inventory_issue(
                        "codex_rollout_identity_unresolved",
                        unresolved_identity=True,
                    )
                    continue
                if canonical_path in indexed_paths:
                    continue
                identity = _read_codex_rollout_identity(source)
                if identity is None:
                    record_inventory_issue(
                        "codex_rollout_identity_unresolved",
                        unresolved_identity=True,
                    )
                    continue
                session_id = str(identity["session_id"])
                if session_id in indexed_session_ids:
                    continue
                row = {
                    "id": session_id,
                    "rollout_path": str(source.path),
                    "created_at": identity.get("created_at") or int(source.mtime),
                    "updated_at": int(source.mtime),
                    "cwd": identity.get("cwd"),
                    "tokens_used": 0,
                    "model": None,
                    "cli_version": None,
                    "source": "rollout_only",
                    "thread_source": "rollout_only",
                    "_rollout_parent_thread_id": identity.get("parent_session_id"),
                    "_source_revision_at": source.mtime_ns,
                }
                previous = rollout_only_by_session.get(session_id)
                if previous is None or _safe_nonnegative_int(
                    row.get("updated_at")
                ) > _safe_nonnegative_int(previous.get("updated_at")):
                    rollout_only_by_session[session_id] = row
        except _ClientUsageDiscoveryReadError as exc:
            record_inventory_issue(
                exc.code,
                unresolved_identity=False,
            )
            continue
        except OSError:
            record_inventory_issue(
                "codex_rollout_inventory_failed",
                unresolved_identity=False,
            )
            continue
        finally:
            if transcript_root_fd is not None:
                os.close(transcript_root_fd)
    return list(rollout_only_by_session.values())


def _open_regular_source_file_fd(
    path: Path,
    *,
    root: Path,
) -> tuple[int, Path, Path, os.stat_result]:
    """Open ``path`` beneath ``root`` without following descendant symlinks.

    Source discovery and import must agree on the same trust boundary.  A
    final-component ``is_file()`` check is insufficient because it follows a
    symlink before the importer assigns the row to the configured home.  Walk
    each descendant relative to an already-open directory, use O_NOFOLLOW at
    every step, and retain the observed inode for a post-open SQLite check.
    """

    root_path = Path(os.path.abspath(os.fspath(root.expanduser())))
    file_path = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        relative = file_path.relative_to(root_path)
    except ValueError as exc:
        raise OSError("local source escaped configured root") from exc
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError("local source path is invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        root_fd, _root_stat = _open_directory_root_fd_no_follow(root_path)
        directory_fds.append(root_fd)
        for component in parts[:-1]:
            directory_fds.append(
                os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_fds[-1],
                )
            )
        file_fd = os.open(
            parts[-1],
            file_flags,
            dir_fd=directory_fds[-1],
        )
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("local source is not a regular file")
        opened_fd = file_fd
        file_fd = None
        return opened_fd, root_path, file_path, file_stat
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _regular_source_file(path: Path, *, root: Path) -> _RegularSourceFile | None:
    try:
        file_fd, root_path, file_path, file_stat = _open_regular_source_file_fd(
            path,
            root=root,
        )
    except OSError:
        return None
    try:
        return _RegularSourceFile(
            path=file_path,
            root=root_path,
            mtime=float(file_stat.st_mtime),
            mtime_ns=int(file_stat.st_mtime_ns),
            device=int(file_stat.st_dev),
            inode=int(file_stat.st_ino),
            size=int(file_stat.st_size),
        )
    finally:
        os.close(file_fd)


def _open_regular_source_text(source: _RegularSourceFile):
    """Re-open a previously discovered regular source without symlinks."""

    file_fd, _root_path, _file_path, opened_stat = _open_regular_source_file_fd(
        source.path,
        root=source.root,
    )
    if (
        int(opened_stat.st_dev) != source.device
        or int(opened_stat.st_ino) != source.inode
    ):
        os.close(file_fd)
        raise OSError("local source changed during open")
    return os.fdopen(file_fd, "r", encoding="utf-8", errors="replace")


def _connect_regular_source_sqlite_read_only(
    source: _RegularSourceFile,
) -> sqlite3.Connection:
    """Connect only when the configured SQLite path remains the same inode."""

    file_fd, _root_path, file_path, opened_stat = _open_regular_source_file_fd(
        source.path,
        root=source.root,
    )
    try:
        if (
            int(opened_stat.st_dev) != source.device
            or int(opened_stat.st_ino) != source.inode
        ):
            raise OSError("local SQLite source changed before read")
        con = sqlite3.connect(f"{file_path.as_uri()}?mode=ro", uri=True)
        observed = file_path.lstat()
        if (
            not stat.S_ISREG(observed.st_mode)
            or int(observed.st_dev) != source.device
            or int(observed.st_ino) != source.inode
        ):
            raise OSError("local SQLite source changed during connect")
        return con
    except BaseException:
        if "con" in locals():
            con.close()
        raise
    finally:
        os.close(file_fd)


def _canonical_source_file_path(path: Path) -> str:
    try:
        return str(path.resolve(strict=True))
    except (OSError, RuntimeError):
        return os.path.abspath(os.fspath(path))


def _source_file_revision_ns(path: Path) -> int | None:
    try:
        file_stat = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        return None
    return int(file_stat.st_mtime_ns)


def _codex_rollout_source(
    path: Path,
    *,
    codex_root: Path,
) -> _RegularSourceFile | None:
    """Accept indexed rollouts only from Codex's two owned transcript roots."""

    if not path.is_absolute():
        return None
    for allowed_root in (
        codex_root / "sessions",
        codex_root / "archived_sessions",
    ):
        source = _regular_source_file(path, root=allowed_root)
        if source is not None:
            return source
    return None


def _coerce_private_regular_source(
    source: _RegularSourceFile | Path,
) -> _RegularSourceFile | None:
    """Keep direct parser callers compatible without weakening import roots."""

    if isinstance(source, _RegularSourceFile):
        return source
    return _regular_source_file(source, root=source.parent)


def _read_codex_rollout_identity(
    source: _RegularSourceFile | Path,
) -> dict[str, Any] | None:
    """Read the first authoritative Codex ``session_meta`` identity."""

    source_file = _coerce_private_regular_source(source)
    if source_file is None:
        return None
    try:
        with _open_regular_source_text(source_file) as handle:
            for line_number, line in enumerate(handle):
                if line_number >= 64:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    return None
                session_id = _limited_optional_text(payload.get("id"), 512)
                if session_id is None:
                    return None
                parent = _limited_optional_text(payload.get("parent_thread_id"), 512)
                if parent == session_id:
                    parent = None
                return {
                    "session_id": session_id,
                    "parent_session_id": parent,
                    "cwd": _limited_optional_text(payload.get("cwd"), 240),
                    "created_at": _timestamp_seconds(obj.get("timestamp")),
                }
    except OSError:
        return None
    return None


def _read_codex_thread_rows(
    db_source: _RegularSourceFile,
    *,
    limit_sessions: int | None,
) -> list[dict[str, Any]]:
    con = _connect_regular_source_sqlite_read_only(db_source)
    con.row_factory = sqlite3.Row
    try:
        columns = _sqlite_table_columns(con, "threads")
        optional = [
            "source",
            "thread_source",
        ]
        select_columns = [
            "id",
            "rollout_path",
            "created_at",
            "updated_at",
            "cwd",
            "tokens_used",
            "model",
            "cli_version",
            *(column for column in optional if column in columns),
        ]
        query = f"""
            select {", ".join(select_columns)}
            from threads
            order by updated_at desc
        """
        if limit_sessions is None:
            rows = con.execute(query).fetchall()
        else:
            rows = con.execute(f"{query} limit ?", (limit_sessions,)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.DatabaseError as exc:
        raise _ClientUsageDiscoveryReadError("sqlite_thread_scan_failed") from exc
    finally:
        con.close()


def _read_codex_session_titles(
    index_source: _RegularSourceFile,
    *,
    session_ids: set[str],
) -> dict[str, str]:
    """Read explicit Codex sidebar names for already-selected usage sessions.

    The index is enrichment only: it cannot add or reorder sessions selected
    from sqlite. Files are streamed, malformed rows are ignored, and the last
    valid string wins so a later sidebar rename supersedes the old name.
    Sanitization remains centralized in ``ClientUsageEvent.to_sentinel_event``
    so an explicit but non-printable title is recorded as redacted rather than
    silently resurrecting an older value.
    """

    if not session_ids:
        return {}
    titles: dict[str, str] = {}
    try:
        with _open_regular_source_text(index_source) as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                session_id = row.get("id")
                title = row.get("thread_name")
                if isinstance(session_id, str) and session_id in session_ids and isinstance(title, str):
                    titles[session_id] = title
    except OSError:
        return {}
    return titles


def _read_codex_spawn_edges(
    db_source: _RegularSourceFile,
) -> dict[str, dict[str, Any]]:
    con = _connect_regular_source_sqlite_read_only(db_source)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select parent_thread_id, child_thread_id, status
            from thread_spawn_edges
            """
        ).fetchall()
        return {str(row["child_thread_id"]): dict(row) for row in rows if row["child_thread_id"]}
    except sqlite3.DatabaseError:
        return {}
    finally:
        con.close()


def _sqlite_table_columns(con: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"pragma table_info({table_name})").fetchall()}


def _codex_session_kind(row: dict[str, Any], *, parent_session_id: str | None) -> str:
    if parent_session_id:
        return "child"
    thread_source = str(row.get("thread_source") or "").lower()
    source = str(row.get("source") or "").lower()
    if thread_source == "subagent" or '"subagent"' in source:
        return "child"
    return "root"


def _codex_edge_is_real_spawn(
    row: dict[str, Any],
    *,
    spawn_edge: dict[str, Any],
    usage: dict[str, Any] | None,
) -> bool:
    """Whether a recorded parent is a concurrent spawn, not continued lineage.

    Codex writes a ``parent_thread_id`` on fork / resume / compaction rollouts
    — the SAME conversation continued — exactly as it does on a genuinely
    spawned child. Only a proven spawn may carry a Task-grouping parent;
    otherwise transitively chaining those lineage pointers merges unrelated
    conversations into one mega-Task. The signals that prove a distinct
    concurrent child are:
      * a ``thread_spawn_edges`` row (Codex's canonical spawn table),
      * a subagent thread (its own thread_source / source marker), or
      * a rollout that recorded ``source.subagent.thread_spawn``.
    Internal auto-review threads are spawned children too; the caller carries
    them via ``is_internal_review``. A bare lineage ``parent_thread_id`` from
    fork / resume / compaction matches none of these and becomes its own root.
    """

    if _limited_optional_text(spawn_edge.get("parent_thread_id"), 240):
        return True
    thread_source = str(row.get("thread_source") or "").lower()
    source = str(row.get("source") or "").lower()
    if thread_source == "subagent" or '"subagent"' in source:
        return True
    if isinstance(usage, dict) and usage.get("_spawn_source_detected"):
        return True
    return False


def _codex_apply_session_kind_overrides(
    kind: str,
    *,
    lineage_parent_session_id: str | None,
    usage: dict[str, Any] | None,
    is_internal_review: bool,
) -> str:
    """Apply the replay-carrier and internal-review overrides to a base kind.

    Both the token-lineage kind and the Task-grouping kind share these two
    corrections, and both gate the replay override on the EXACT recorded
    (lineage) parent: a fork/resume that carries an exact parent is a clean
    root, so only a replay carrier with no exact parent at all is forced to
    ``child``. An internal auto-review label always wins last.
    """

    if (
        lineage_parent_session_id is None
        and isinstance(usage, dict)
        and (
            usage.get("_replay_source_detected")
            or usage.get("_replayed_parent_session_meta")
        )
    ):
        kind = "child"
    if is_internal_review:
        kind = "internal"
    return kind


def _codex_row_session_id(row: dict[str, Any]) -> str:
    row_id = str(row.get("id") or "")
    if row_id:
        return row_id
    return Path(str(row.get("rollout_path") or "")).stem


def _select_codex_root_groups(
    rows: list[dict[str, Any]],
    *,
    parent_by_session: dict[str, str | None],
    limit_root_groups: int,
) -> tuple[list[dict[str, Any]], int]:
    """Select recent Codex conversations without truncating their lineage.

    ``limit_root_groups`` counts top-level conversation groups. Activity on a
    descendant makes the whole group recent, and every discovered row in a
    selected group is returned. An orphan whose recorded parent is unavailable
    remains its own group; linkage is never guessed from timestamps or cwd.
    """

    if limit_root_groups <= 0 or not rows:
        return [], 0
    session_ids = {_codex_row_session_id(row) for row in rows}
    root_by_session: dict[str, str] = {}

    def resolve_root(session_id: str) -> str:
        cached = root_by_session.get(session_id)
        if cached is not None:
            return cached
        trail: list[str] = []
        current = session_id
        while current not in root_by_session:
            if current in trail:
                # Corrupt cycles must stay together but must not hang import.
                cycle_start = trail.index(current)
                root_id = min(trail[cycle_start:])
                break
            trail.append(current)
            parent_id = parent_by_session.get(current)
            if parent_id is None or parent_id not in session_ids:
                root_id = current
                break
            current = parent_id
        else:
            root_id = root_by_session[current]
        for visited in trail:
            root_by_session[visited] = root_id
        return root_id

    activity_by_root: dict[str, int] = {}
    for row in rows:
        root_id = resolve_root(_codex_row_session_id(row))
        activity_by_root[root_id] = max(
            activity_by_root.get(root_id, 0),
            _optional_int(row.get("updated_at")) or 0,
        )
    selected_roots = {
        root_id
        for root_id, _activity in sorted(
            activity_by_root.items(),
            key=lambda item: (-item[1], item[0]),
        )[:limit_root_groups]
    }
    selected_rows = [row for row in rows if resolve_root(_codex_row_session_id(row)) in selected_roots]
    return selected_rows, len(selected_roots)


def _codex_model_label(value: Any) -> str | None:
    text = _limited_optional_text(value, 120)
    if not text:
        return None
    if _is_codex_internal_review_model(text):
        return None
    return text


def _is_codex_internal_review_model(value: Any) -> bool:
    text = _limited_optional_text(value, 120)
    if not text:
        return False
    normalized = text.lower().replace("_", "-")
    return normalized.startswith("codex-auto-") or normalized in {"codex-review", "codex-approval-review"}


_CODEX_TOKEN_COUNTER_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _codex_counter_source_present(value: object, field_name: str) -> bool:
    if not isinstance(value, dict) or field_name not in value:
        return False
    counter = value.get(field_name)
    return (
        isinstance(counter, int)
        and not isinstance(counter, bool)
        and counter >= 0
    )


def _codex_counter_presence(value: object) -> tuple[bool, bool, bool, bool, bool, bool]:
    return tuple(
        _codex_counter_source_present(value, field_name)
        for field_name in (*_CODEX_TOKEN_COUNTER_FIELDS, "total_tokens")
    )  # type: ignore[return-value]


def _codex_counter_reported(usage: dict[str, Any], field_name: str) -> bool:
    """Return source presence retained before numeric normalization.

    New reader results carry an internal boolean captured from the final raw
    cumulative counter object.  The membership fallback keeps direct unit-test
    fixtures and older in-process callers compatible without interpreting a
    numeric zero as absence.
    """

    retained = usage.get(f"_{field_name}_reported")
    if isinstance(retained, bool):
        return retained
    return _codex_counter_source_present(usage, field_name)


def _codex_normalized_input_reported(usage: dict[str, Any]) -> bool:
    """Prove agentacct's input remainder from every applicable source split.

    Codex ``input_tokens`` is inclusive. ``cached_input_tokens`` is the cache-
    read split in the current rollout representation and must therefore be
    reported before the remainder can be truth. ``cache_write_tokens`` is an
    optional representation capability: complete absence means not
    applicable, but once any source carrier exposes it, invalid or incomplete
    presence must fail closed instead of becoming a compatibility zero.
    """

    if not (
        _codex_counter_reported(usage, "input_tokens")
        and _codex_counter_reported(usage, "cached_input_tokens")
    ):
        return False
    applicability = usage.get("_cache_write_tokens_applicable")
    if applicability is None:
        cache_write_applicable = "cache_write_tokens" in usage
    elif isinstance(applicability, bool):
        cache_write_applicable = applicability
    else:
        return False
    return not cache_write_applicable or _codex_counter_reported(
        usage,
        "cache_write_tokens",
    )


def _codex_last_token_usage_schema_drift(
    *,
    current_value: object,
    last_value: object,
) -> bool:
    """Reject last counters that cannot be a delta of the current total."""

    if not isinstance(current_value, dict) or not isinstance(last_value, dict):
        return True
    for field_name in (*_CODEX_TOKEN_COUNTER_FIELDS, "total_tokens"):
        if field_name not in last_value:
            continue
        if not (
            _codex_counter_source_present(current_value, field_name)
            and _codex_counter_source_present(last_value, field_name)
        ):
            return True
        if int(last_value[field_name]) > int(current_value[field_name]):
            return True
    return False


def _codex_token_delta_presence(
    *,
    current_value: object,
    last_value: object,
    previous_value: object,
) -> tuple[bool, bool, bool, bool, bool, bool]:
    """Presence proof for each numeric delta returned by `_codex_token_delta`."""

    reset = _codex_cumulative_counter_reset(
        current_value=current_value,
        previous_value=previous_value,
    )
    current_presence = _codex_counter_presence(current_value)
    previous_presence = _codex_counter_presence(previous_value)
    last_presence = _codex_counter_presence(last_value)
    last = last_value if isinstance(last_value, dict) else None
    field_names = (*_CODEX_TOKEN_COUNTER_FIELDS, "total_tokens")
    return tuple(
        last_presence[position]
        if last is not None and field_name in last
        else current_presence[position]
        if reset
        else current_presence[position] and previous_presence[position]
        for position, field_name in enumerate(field_names)
    )  # type: ignore[return-value]


def _codex_timestamp_second(value: object) -> str | None:
    text = value if isinstance(value, str) else ""
    return text[:19] if len(text) >= 19 else None


def _codex_session_meta_has_replay_source(payload: dict[str, Any]) -> bool:
    """Detect Codex's structured fork carriers without reading message text."""

    pending: list[object] = [payload.get("source"), payload.get("originator")]
    visited = 0
    while pending and visited < 64:
        value = pending.pop()
        visited += 1
        if isinstance(value, dict):
            if any(key in value for key in ("thread_spawn", "forked_from_id")):
                return True
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def _codex_session_meta_has_spawn_source(payload: dict[str, Any]) -> bool:
    """Detect a Codex concurrent-spawn carrier (``source.subagent.thread_spawn``).

    Strictly narrower than :func:`_codex_session_meta_has_replay_source`: a
    fork records ``forked_from_id`` and a resume / compaction records neither,
    and both merely continue the SAME conversation — they must not carry a
    Task-grouping parent. Only a ``thread_spawn`` marker denotes a distinct
    concurrent child that legitimately nests under its spawning root.
    """

    pending: list[object] = [payload.get("source"), payload.get("originator")]
    visited = 0
    while pending and visited < 64:
        value = pending.pop()
        visited += 1
        if isinstance(value, dict):
            if "thread_spawn" in value:
                return True
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def _codex_counter_vector(value: object) -> tuple[int, int, int, int, int, int]:
    counters = value if isinstance(value, dict) else {}

    def counter_value(field_name: str) -> int:
        return (
            int(counters[field_name])
            if _codex_counter_source_present(counters, field_name)
            else 0
        )

    input_tokens = counter_value("input_tokens")
    cached_input_tokens = counter_value("cached_input_tokens")
    cache_write_tokens = counter_value("cache_write_tokens")
    output_tokens = counter_value("output_tokens")
    reasoning_output_tokens = counter_value("reasoning_output_tokens")
    total_tokens = counter_value("total_tokens")
    if (
        not _codex_counter_source_present(counters, "total_tokens")
        and input_tokens + output_tokens > 0
    ):
        total_tokens = input_tokens + output_tokens
    return (
        input_tokens,
        cached_input_tokens,
        cache_write_tokens,
        output_tokens,
        reasoning_output_tokens,
        total_tokens,
    )


def _codex_cumulative_counter_reset(
    *,
    current_value: object,
    previous_value: object,
) -> bool:
    """Return true only when comparable source counters prove an epoch reset."""

    if not isinstance(previous_value, dict):
        return True
    current = _codex_counter_vector(current_value)
    previous = _codex_counter_vector(previous_value)
    current_presence = _codex_counter_presence(current_value)
    previous_presence = _codex_counter_presence(previous_value)
    return any(
        current_presence[position]
        and previous_presence[position]
        and current[position] < previous[position]
        for position in range(len(current))
    )


def _codex_retained_delta_start_proven(
    *,
    current: object,
    last: object,
    previous: object,
) -> bool:
    """Prove the first retained event is local rather than inherited total."""

    if isinstance(previous, dict):
        return True
    if not isinstance(current, dict) or not isinstance(last, dict):
        return False
    compared = False
    for field_name in _CODEX_TOKEN_COUNTER_FIELDS:
        if field_name not in current:
            continue
        current_value = current.get(field_name)
        last_value = last.get(field_name)
        if (
            not isinstance(current_value, int)
            or isinstance(current_value, bool)
            or current_value < 0
            or not isinstance(last_value, int)
            or isinstance(last_value, bool)
            or last_value < 0
            or last_value > current_value
        ):
            return False
        compared = True
    return compared


def _codex_token_delta(
    *,
    current_value: object,
    last_value: object,
    previous_value: object,
) -> tuple[int, int, int, int, int, int]:
    current = _codex_counter_vector(current_value)
    previous = (
        _codex_counter_vector(previous_value)
        if isinstance(previous_value, dict)
        else (0, 0, 0, 0, 0, 0)
    )
    reset = _codex_cumulative_counter_reset(
        current_value=current_value,
        previous_value=previous_value,
    )
    current_presence = _codex_counter_presence(current_value)
    previous_presence = _codex_counter_presence(previous_value)
    last = last_value
    if isinstance(last, dict):
        field_names = (*_CODEX_TOKEN_COUNTER_FIELDS, "total_tokens")
        last_vector = _codex_counter_vector(last)
        return tuple(
            last_vector[position]
            if field_name in last
            else current[position]
            if reset
            else 0
            if not current_presence[position]
            else current[position]
            if not previous_presence[position]
            else current[position] - previous[position]
            for position, field_name in enumerate(field_names)
        )  # type: ignore[return-value]

    # A cumulative reset starts a new local epoch.  Treat the current counters
    # as that epoch's first delta instead of manufacturing negative usage.
    if reset:
        return current
    return tuple(
        0
        if not current_presence[position]
        else current[position]
        if not previous_presence[position]
        else current[position] - previous[position]
        for position in range(len(current))
    )  # type: ignore[return-value]


def _codex_lineage_depth(
    session_id: str,
    *,
    parent_by_session: dict[str, str | None],
) -> int:
    depth = 0
    current = session_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        parent = parent_by_session.get(current)
        if parent is None or parent not in parent_by_session:
            break
        depth += 1
        current = parent
    return depth


def _codex_lineage_root(
    session_id: str,
    *,
    parent_by_session: dict[str, str | None],
) -> str:
    current = session_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        parent = parent_by_session.get(current)
        if parent is None or parent not in parent_by_session:
            return current
        current = parent
    return min(seen)


def _codex_lineage_has_cycle(
    session_id: str,
    *,
    parent_by_session: dict[str, str | None],
) -> bool:
    current = session_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        parent = parent_by_session.get(current)
        if parent is None or parent not in parent_by_session:
            return False
        current = parent
    return True


def _normalize_codex_rollout_usage_cohorts(
    rows: list[dict[str, Any]],
    *,
    parent_by_session: dict[str, str | None],
    usage_by_session: dict[str, dict[str, Any] | None],
    parse_complete_by_session: dict[str, bool],
) -> None:
    """Replace replayed cumulative finals with unique per-turn cohort deltas.

    Codex forks can copy the parent's token-count sequence into the child
    rollout. Following ccusage v20.0.14, two first-second token events prove a
    replay second; only the later suffix is retained. Exact duplicate deltas
    are then removed inside the complete root group. Missing/conflicting
    parents, cycles, malformed sources, and incomplete parses stay untouched;
    the shared additivity gate continues to hold those raw rows.
    """

    rows_by_session = {
        session_id: row
        for row in rows
        if (session_id := _codex_row_session_id(row))
    }
    sessions_by_root: dict[str, list[str]] = {}
    for session_id in rows_by_session:
        root = _codex_lineage_root(
            session_id,
            parent_by_session=parent_by_session,
        )
        sessions_by_root.setdefault(root, []).append(session_id)

    for session_ids in sessions_by_root.values():
        seen_event_signatures: set[tuple[Any, ...]] = set()
        session_ids.sort(
            key=lambda session_id: (
                _codex_lineage_depth(
                    session_id,
                    parent_by_session=parent_by_session,
                ),
                _optional_int(rows_by_session[session_id].get("created_at")) or 0,
                session_id,
            )
        )
        for session_id in session_ids:
            if _codex_lineage_has_cycle(
                session_id,
                parent_by_session=parent_by_session,
            ):
                continue
            usage = usage_by_session.get(session_id)
            if not isinstance(usage, dict):
                continue
            events = usage.get("_token_usage_events")
            if not isinstance(events, list):
                continue
            raw_token_event_count = _safe_nonnegative_int(
                usage.get("_raw_token_event_count")
            )
            if raw_token_event_count <= 0:
                continue
            if not parse_complete_by_session.get(session_id, False):
                continue

            parent_id = parent_by_session.get(session_id)
            replay_prefix = _safe_nonnegative_int(
                usage.get("_replay_prefix_token_events")
            )
            replay_baseline: tuple[int, int, int, int, int, int] | None = None
            if parent_id is not None:
                rollout_parent_id = _limited_optional_text(
                    usage.get("session_meta_parent_thread_id"),
                    240,
                )
                if (
                    rollout_parent_id is not None
                    and rollout_parent_id != parent_id
                ):
                    # Two authoritative Codex carriers disagree. Never use
                    # one carrier's token shape while assigning the row to
                    # the other carrier's Task/root lineage.
                    continue
                parent_usage = usage_by_session.get(parent_id)
                if (
                    not isinstance(parent_usage, dict)
                    or not parse_complete_by_session.get(parent_id, False)
                ):
                    continue
                replay_source_marker = bool(
                    usage.get("_replay_source_detected")
                )
                replayed_parent_meta = bool(
                    usage.get("_replayed_parent_session_meta")
                )
                replay_marker = replay_source_marker or replayed_parent_meta
                if (
                    replayed_parent_meta
                    and not replay_source_marker
                    and replay_prefix <= 0
                ):
                    # A replayed exact-parent meta without the ordinary
                    # thread_spawn carrier is schema drift. Unless a repeated
                    # first-second prefix independently proves the replay
                    # boundary, do not release this descendant.
                    continue
                if replay_marker and replay_prefix > 0:
                    baseline = usage.get("_replay_baseline")
                    if not isinstance(baseline, dict):
                        # A fork carrier without a creation-second baseline is
                        # schema drift. Preserve the raw row and hold it.
                        continue
                    replay_baseline = _codex_counter_vector(baseline)
                elif not bool(usage.get("_retained_delta_start_proven")):
                    # A child can start with an inherited cumulative counter
                    # but still provide a complete per-turn last_token_usage.
                    # Without that (or a zero-based first turn), its local
                    # delta start is unproven and the raw row stays held.
                    continue

            raw_latest = _codex_counter_vector(usage)
            session_model = _codex_model_label(
                usage.get("model") or rows_by_session[session_id].get("model")
            )
            kept_deltas: list[
                tuple[
                    tuple[int, int, int, int, int, int],
                    tuple[bool, bool, bool, bool, bool, bool],
                ]
            ] = []
            exact_duplicates = 0
            for event in events:
                if (
                    not isinstance(event, tuple)
                    or len(event) != 5
                    or not isinstance(event[2], tuple)
                    or len(event[2]) != 6
                    or not isinstance(event[4], tuple)
                    or len(event[4]) != 6
                ):
                    continue
                timestamp, raw_event_model, delta, _start_proven, presence = event
                event_model = _codex_model_label(raw_event_model) or session_model
                signature = (
                    str(timestamp),
                    event_model,
                    *delta,
                    *presence,
                ) if isinstance(timestamp, str) and timestamp else None
                if signature is not None and signature in seen_event_signatures:
                    exact_duplicates += 1
                    continue
                if signature is not None:
                    seen_event_signatures.add(signature)
                kept_deltas.append((delta, presence))

            sums = tuple(
                sum(delta[position] for delta, _presence in kept_deltas)
                for position in range(6)
            )
            reported = tuple(
                bool(kept_deltas)
                and all(presence[position] for _delta, presence in kept_deltas)
                for position in range(6)
            )
            usage.update(
                {
                    "input_tokens": sums[0],
                    "_input_tokens_reported": reported[0],
                    "cached_input_tokens": sums[1],
                    "_cached_input_tokens_reported": reported[1],
                    "_cache_write_tokens_reported": reported[2],
                    "output_tokens": sums[3],
                    "_output_tokens_reported": reported[3],
                    "reasoning_output_tokens": sums[4],
                    "_reasoning_output_tokens_reported": reported[4],
                    "total_tokens": sums[5],
                    "_total_tokens_reported": reported[5],
                    "turn_count": len(kept_deltas),
                    "raw_usage_rows": raw_token_event_count,
                    "deduplicated_usage_rows": exact_duplicates,
                    "raw_cumulative_input_tokens": raw_latest[0],
                    "raw_cumulative_cached_input_tokens": raw_latest[1] + raw_latest[2],
                    "raw_cumulative_output_tokens": raw_latest[3],
                    "raw_cumulative_reasoning_output_tokens": raw_latest[4],
                    "replay_prefix_token_events": replay_prefix,
                    "_normalized_usage": True,
                }
            )
            if reported[2]:
                usage["cache_write_tokens"] = sums[2]
            else:
                # Do not let an invalid source value or a compatibility zero
                # survive normalization as a measured cache-write counter.
                usage.pop("cache_write_tokens", None)
            if parent_id is not None:
                usage["usage_update_semantics"] = CODEX_LINEAGE_DELTA_SEMANTICS
            if replay_baseline is not None:
                usage.update(
                    {
                        "replay_baseline_input_tokens": replay_baseline[0],
                        "replay_baseline_cached_input_tokens": replay_baseline[1]
                        + replay_baseline[2],
                        "replay_baseline_output_tokens": replay_baseline[3],
                        "replay_baseline_reasoning_output_tokens": replay_baseline[4],
                    }
                )
    for usage in usage_by_session.values():
        if isinstance(usage, dict):
            usage.pop("_token_usage_events", None)


def _read_codex_rollout_parent_thread_id(
    source: _RegularSourceFile | Path,
) -> str | None:
    """Read only the first authoritative session_meta parent from a rollout."""

    source_file = _coerce_private_regular_source(source)
    if source_file is None:
        return None
    try:
        with _open_regular_source_text(source_file) as handle:
            # Codex writes session_meta at rollout creation (normally line 1).
            # Bound this lightweight lineage probe so a malformed historical
            # file without session_meta cannot turn every watch tick into a
            # full-history JSONL scan. Missing linkage remains an honest orphan.
            for line_number, line in enumerate(handle):
                if line_number >= 64:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    return None
                parent = payload.get("parent_thread_id")
                own_id = payload.get("id")
                return parent if isinstance(parent, str) and parent and parent != own_id else None
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Codex rollout parse memoization.
#
# The dominant per-refresh cost is re-parsing every selected codex rollout file
# even when its bytes have not changed since the last scan (a completed
# session's rollout is immutable). This cache memoizes the parse result keyed
# by the file's identity (path, mtime_ns, size, inode) AND a hash of the
# parsing code. A cache HIT is therefore only ever an identical re-parse of
# identical bytes with identical code, so the emitted ClientUsageEvents are
# byte-identical to the no-cache path — completeness, deletion inference, and
# every downstream reconcile are unaffected. Everything is fail-open: any load
# / save / lookup / decode error falls back to a full parse.
# ---------------------------------------------------------------------------

_CODEX_PARSE_CACHE_FORMAT = 1
_CODEX_PARSE_CACHE_FILENAME = "codex_rollout_parse.pkl"
_CODEX_PARSE_CACHE_DISABLE_ENV = "AGENTACCT_CODEX_PARSE_CACHE"
# Cap on retained entries. A machine has a bounded number of codex rollout files
# (typically a few thousand), so this is only a runaway backstop; it is large
# enough that the common case never prunes, which keeps the cache warm across a
# limit-20 watcher and a limit-500 dashboard scan sharing one store (pruning to
# only the touched set would let the watcher evict the dashboard's entries).
_CODEX_PARSE_CACHE_MAX_ENTRIES = 20000
_CODEX_PARSER_CODE_VERSION: str | None = None


def _codex_parser_code_version() -> str | None:
    """A version tag that changes whenever the parsing code changes, so a stale
    cache from an older parser is never trusted. Hashes the installed package
    version, a manual format counter, and this module plus its rollout-adapter
    and evidence dependencies.
    Returns None when the source cannot be read (frozen bundle / .pyc-only): in
    that case the version cannot be proven, so the cache is disabled rather than
    keyed on a constant 'missing' tag that would survive an upgrade."""

    global _CODEX_PARSER_CODE_VERSION
    if _CODEX_PARSER_CODE_VERSION is None:
        hasher = hashlib.sha256()
        hasher.update(str(_CODEX_PARSE_CACHE_FORMAT).encode())
        try:
            from importlib.metadata import PackageNotFoundError, version as _pkg_version

            try:
                hasher.update(str(_pkg_version("agentacct")).encode())
            except PackageNotFoundError:
                try:
                    hasher.update(str(_pkg_version("agent-chronicle")).encode())
                except PackageNotFoundError:
                    hasher.update(b"\0nopkgver\0")
        except Exception:  # noqa: BLE001 - metadata is best-effort.
            hasher.update(b"\0nopkgver\0")
        readable = True
        for module_path in (
            Path(__file__),
            Path(__file__).with_name("codex_rollout_adapter.py"),
            Path(__file__).with_name("log_evidence.py"),
            Path(__file__).with_name("tool_activity.py"),
        ):
            try:
                hasher.update(module_path.read_bytes())
            except OSError:
                readable = False
        # "" is a memoized "disabled" marker (distinct from the None recompute
        # sentinel); the accessor maps it back to None.
        _CODEX_PARSER_CODE_VERSION = hasher.hexdigest()[:16] if readable else ""
    return _CODEX_PARSER_CODE_VERSION or None


class _CachePlainDataUnpickler(pickle.Unpickler):
    """Unpickle only plain data. The cache holds dicts/lists/tuples/sets and
    scalars, which the pickle VM builds from opcodes without ever calling
    ``find_class``; any find_class request therefore means the file tried to
    construct a class (a poisoned cache / ``__reduce__`` RCE) and is refused.
    This is defense-in-depth on top of the store's owner-only (0700) perms."""

    def find_class(self, module: str, name: str) -> Any:  # noqa: D401
        raise pickle.UnpicklingError(f"blocked class in codex parse cache: {module}.{name}")


class _CodexRolloutParseCache:
    """Persistent, code-versioned memo of the per-rollout parse. Fail-open."""

    def __init__(self, path: Path, code_version: str, entries: dict[Any, Any]) -> None:
        self._path = path
        self._code_version = code_version
        self._entries = entries
        self._touched: set[Any] = set()
        self._dirty = False
        self.hits = 0
        self.misses = 0

    @classmethod
    def load(cls, cache_dir: Path, code_version: str) -> "_CodexRolloutParseCache":
        path = cache_dir / _CODEX_PARSE_CACHE_FILENAME
        entries: dict[Any, Any] = {}
        try:
            with open(path, "rb") as handle:
                blob = _CachePlainDataUnpickler(handle).load()
            if isinstance(blob, dict) and blob.get("version") == code_version:
                stored = blob.get("entries")
                if isinstance(stored, dict):
                    entries = stored
        except (OSError, pickle.PickleError, EOFError, AttributeError, ValueError, ImportError):
            entries = {}
        return cls(path, code_version, entries)

    def get(self, key: Any) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._touched.add(key)
        self.hits += 1
        return entry

    def put(self, key: Any, value: Any) -> None:
        self._entries[key] = value
        self._touched.add(key)
        self._dirty = True

    def save(self) -> None:
        # Persist the whole working set (not just what this scan touched) so a
        # limit-20 watcher does not evict a limit-500 dashboard's entries. Only
        # an absurdly large cache is pruned, and then this run's touched entries
        # are kept first.
        entries = self._entries
        if len(entries) > _CODEX_PARSE_CACHE_MAX_ENTRIES:
            kept = {key: entries[key] for key in self._touched if key in entries}
            for key, value in entries.items():
                if len(kept) >= _CODEX_PARSE_CACHE_MAX_ENTRIES:
                    break
                kept.setdefault(key, value)
            entries = kept
        if not self._dirty and len(entries) == len(self._entries):
            return
        try:
            cache_dir = self._path.parent
            cache_dir.mkdir(parents=True, exist_ok=True)
            # Match the store root's owner-only posture: the memo is derived from
            # private client logs, so keep the directory 0700 too (not the 0755
            # mkdir default), even though the 0700 store root already shadows it.
            try:
                os.chmod(cache_dir, 0o700)
            except OSError:
                pass
            tmp = self._path.with_name(self._path.name + ".tmp")
            with open(tmp, "wb") as handle:
                pickle.dump(
                    {"version": self._code_version, "entries": entries},
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError:
            pass


_CODEX_PARSE_CACHE: contextvars.ContextVar["_CodexRolloutParseCache | None"] = (
    contextvars.ContextVar("codex_parse_cache", default=None)
)


@contextmanager
def codex_parse_cache_scope(
    store_dir: Path | str | None, *, enabled: bool = True
) -> Iterator["_CodexRolloutParseCache | None"]:
    """Activate the codex rollout parse cache for one scan. Fail-open; the
    disable env var forces the un-memoized path."""

    disable = (read_env_alias(_CODEX_PARSE_CACHE_DISABLE_ENV) or "").strip().lower()
    if not enabled or store_dir is None or disable in {"0", "false", "off", "no"}:
        yield None
        return
    code_version = _codex_parser_code_version()
    if code_version is None:
        # Parser source is unreadable (e.g. a frozen bundle): we cannot prove a
        # cached entry was produced by this exact code, so refuse to memoize.
        yield None
        return
    try:
        cache = _CodexRolloutParseCache.load(Path(store_dir) / "cache", code_version)
    except Exception:  # noqa: BLE001 - a cache must never break a scan.
        yield None
        return
    token = _CODEX_PARSE_CACHE.set(cache)
    try:
        yield cache
    finally:
        _CODEX_PARSE_CACHE.reset(token)
        try:
            cache.save()
        except Exception:  # noqa: BLE001
            pass


# --- Refresh progress reporting (dashboard live progress) --------------------
# A ContextVar-scoped reporter lets the discovery loops report live scan
# progress WITHOUT threading a callback through every signature (same
# low-invasiveness pattern as the codex parse cache scope above). The reporter
# is any object with ``scan(client, scanned, total)`` and ``phase(label)``.
# Emits are best-effort — a reporter error never affects discovery output — and
# with no scope active every emit is a cheap ContextVar read that returns None.
_USAGE_PROGRESS: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "usage_scan_progress", default=None
)
# Only emit every Nth file so a fast scan does not lock the progress store
# thousands of times; the last file is always emitted so the count lands exact.
_PROGRESS_EMIT_STRIDE = 16


@contextmanager
def usage_progress_scope(reporter: Any) -> Iterator[None]:
    """Activate a refresh progress reporter for the current context/thread."""

    token = _USAGE_PROGRESS.set(reporter)
    try:
        yield
    finally:
        _USAGE_PROGRESS.reset(token)


def _emit_scan_progress(client: str, scanned: int, total: int) -> None:
    reporter = _USAGE_PROGRESS.get()
    if reporter is None:
        return
    try:
        reporter.scan(client, scanned, total)
    except Exception:  # noqa: BLE001 - progress is best-effort, never breaks a scan.
        pass


def _emit_scan_phase(label: str) -> None:
    reporter = _USAGE_PROGRESS.get()
    if reporter is None:
        return
    try:
        reporter.phase(label)
    except Exception:  # noqa: BLE001
        pass


# --- Codex Actions extraction (discovery-side) ------------------------------
# Codex's PreToolUse hook barely fires for its built-in tools, but the rollout
# on disk already records every tool call. These pure helpers derive the same
# Actions signals the claude-code hook path produces (tool categories + names,
# touched files, commands), so a Codex Task's Receipt shows WHAT it did —
# retroactively, from data already scanned for tokens, with no hook dependency.
# Only tool NAMES, cwd-relative touched PATHS, and best-effort-scrubbed COMMANDS
# are derived; never tool output, never an absolute path prefix.

_CODEX_TOOL_ACTIVITY_CARRIER_CAP = 5000
_CODEX_EVIDENCE_FRAGMENT_CAP = 5000
_CODEX_TOOL_ARGUMENT_TEXT_MAX = 1_000_000
_CODEX_APPLY_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$", re.MULTILINE
)
_CODEX_APPLY_PATCH_MOVE_RE = re.compile(
    r"^\*\*\*\s+Move\s+to:\s*(.+?)\s*$", re.MULTILINE
)
# Best-effort ``cmd`` extraction from Codex's JS ``exec`` runtime call, e.g.
# ``tools.exec_command({cmd:"pytest -q", workdir:"…"})``. Matches the FIRST single-
# or double-quoted ``cmd`` literal, honoring backslash escapes (so an embedded
# ``\"`` does not truncate the command mid-string). A template-literal (backtick)
# or otherwise unquoted cmd is SKIPPED — an honest undercount, never a half-parsed
# capture. The clean ``exec_command`` function_call channel (exact JSON ``cmd``)
# is read precisely instead.
_CODEX_EXEC_JS_CMD_RE = re.compile(
    r"""cmd\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"""
)


def _iter_codex_apply_patch_paths(patch_text: Any) -> Iterator[str]:
    if not isinstance(patch_text, str) or "*** " not in patch_text:
        return
    for pattern in (_CODEX_APPLY_PATCH_FILE_RE, _CODEX_APPLY_PATCH_MOVE_RE):
        for match in pattern.finditer(patch_text):
            yield match.group(1)


def _codex_apply_patch_paths(patch_text: Any) -> list[str]:
    """The file paths an ``apply_patch`` touched, parsed from its patch body."""

    return list(_iter_codex_apply_patch_paths(patch_text))


def _codex_command_text(tool_name: str, raw_arguments: Any) -> str | None:
    """The shell command a Codex exec tool ran — exact for ``exec_command`` (JSON
    ``cmd``), best-effort for the JS ``exec`` runtime. Never returns tool output."""

    if (
        not isinstance(raw_arguments, str)
        or not raw_arguments
        or len(raw_arguments) > _CODEX_TOOL_ARGUMENT_TEXT_MAX
    ):
        return None
    if tool_name == "exec_command":
        try:
            parsed = json.loads(raw_arguments)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return None
        if isinstance(parsed, dict):
            cmd = parsed.get("cmd") or parsed.get("command")
            return cmd if isinstance(cmd, str) and cmd.strip() else None
        return None
    if tool_name == "exec":
        match = _CODEX_EXEC_JS_CMD_RE.search(raw_arguments)
        if match:
            return match.group(1) if match.group(1) is not None else match.group(2)
    return None


def _codex_relativize_touched_path(raw_path: str, cwd: str | None) -> str | None:
    """A cwd-relative form of an apply_patch path, or ``None`` if it would carry an
    absolute prefix. Codex writes cwd-relative paths in practice; an absolute path
    is relativized when under cwd and otherwise DROPPED (missing beats a leaked home
    dir / username). Pure string math — never touches the filesystem."""

    text = str(raw_path or "").strip()
    if not text:
        return None
    if not os.path.isabs(text):
        return text  # already relative — _normalize_touched_path is the final gate
    cwd_abs = (
        os.path.normpath(cwd)
        if isinstance(cwd, str) and os.path.isabs(cwd)
        else None
    )
    if cwd_abs is None:
        return None
    try:
        target_abs = os.path.normpath(text)
        if target_abs == cwd_abs or target_abs.startswith(cwd_abs + os.sep):
            rel = os.path.relpath(target_abs, cwd_abs)
            return rel if rel and not rel.startswith("..") else None
    except (ValueError, OSError):
        return None
    return None


@dataclass(frozen=True)
class _CodexToolActivityFact:
    """One bounded Actions fact after raw arguments have been discarded."""

    action_name: str
    command: str | None = None
    has_command_conflict: bool = False


@dataclass(frozen=True)
class _CodexActionCarrier:
    """One ephemeral tool-call carrier decoded from a rollout line."""

    call_id: Any
    action_name: Any
    raw_arguments: Any


@dataclass(frozen=True)
class _CodexToolActivityCarrierIdentity:
    """The bounded identity retained between the two Actions scan phases."""

    call_id: str | None
    action_name: str | None


def _codex_action_carrier(payload: Mapping[str, Any]) -> _CodexActionCarrier | None:
    """Decode a supported Actions carrier without retaining its raw arguments."""

    payload_type = payload.get("type")
    if payload_type in {"function_call", "custom_tool_call"}:
        call_name = payload.get("name")
        action_name = call_name
        raw_arguments = (
            payload.get("arguments")
            if payload_type == "function_call"
            else payload.get("input")
        )
        if payload_type == "function_call":
            action_name = codex_function_action_name(
                payload.get("namespace"),
                call_name,
            )
        return _CodexActionCarrier(
            call_id=payload.get("call_id"),
            action_name=action_name,
            raw_arguments=raw_arguments,
        )
    if payload_type == "mcp_tool_call_end":
        invocation = payload.get("invocation")
        if isinstance(invocation, Mapping):
            return _CodexActionCarrier(
                call_id=payload.get("call_id"),
                action_name=codex_mcp_action_name(
                    invocation.get("server"),
                    invocation.get("tool"),
                ),
                raw_arguments=invocation.get("arguments"),
            )
        return None
    if payload_type == "item_completed":
        item = payload.get("item")
        if isinstance(item, Mapping) and item.get("type") == "McpToolCall":
            return _CodexActionCarrier(
                call_id=item.get("id"),
                action_name=codex_mcp_action_name(
                    item.get("server"),
                    item.get("tool"),
                ),
                raw_arguments=item.get("arguments"),
            )
    return None


def _normalized_codex_action_name(raw_action_name: Any) -> str | None:
    """Return one exact, bounded Actions name without truncating identities."""

    validated_action_name = validated_codex_identifier(
        raw_action_name,
        max_length=120,
    )
    if validated_action_name is None:
        return None
    return validated_action_name


def _merge_codex_action_names(first_name: str, second_name: str) -> str | None:
    """Choose a canonical MCP name over its matching bare legacy name."""

    if first_name == second_name:
        return first_name
    if (
        first_name.startswith("mcp__")
        and first_name.rsplit("__", 1)[-1] == second_name
    ):
        return first_name
    if (
        second_name.startswith("mcp__")
        and second_name.rsplit("__", 1)[-1] == first_name
    ):
        return second_name
    return None


def _merge_codex_tool_activity_facts(
    first_fact: _CodexToolActivityFact,
    second_fact: _CodexToolActivityFact,
) -> _CodexToolActivityFact | None:
    """Merge duplicate representations, or reject an ambiguous call identity."""

    merged_action_name = _merge_codex_action_names(
        first_fact.action_name,
        second_fact.action_name,
    )
    if merged_action_name is None:
        return None
    has_command_conflict = (
        first_fact.has_command_conflict or second_fact.has_command_conflict
    )
    if (
        first_fact.command
        and second_fact.command
        and first_fact.command != second_fact.command
    ):
        has_command_conflict = True
    merged_command = (
        None
        if has_command_conflict
        else first_fact.command or second_fact.command
    )
    return _CodexToolActivityFact(
        action_name=merged_action_name,
        command=merged_command,
        has_command_conflict=has_command_conflict,
    )


class _CodexToolActivityAccumulator:
    """Bounded two-phase Actions projection.

    Phase one reconciles logical call identities and scrubbed commands. Phase two
    revisits only those same source lines and collects paths from calls that are
    still valid. Deferring path selection makes the output independent of whether
    a conflicting duplicate appears before or after another valid call, without
    retaining raw arguments or a carrier-by-path product in memory.
    """

    def __init__(
        self,
        *,
        carrier_cap: int = _CODEX_TOOL_ACTIVITY_CARRIER_CAP,
        touched_path_cap: int = _TOUCHED_FILES_PER_BATCH_MAX,
    ) -> None:
        self._carrier_cap = max(0, carrier_cap)
        self._retained_touched_path_cap = max(0, touched_path_cap)
        self._carrier_identities_by_source_line: dict[
            int,
            _CodexToolActivityCarrierIdentity,
        ] = {}
        self._facts_by_call_id: dict[str, _CodexToolActivityFact | None] = {}
        self._anonymous_call_facts_by_source_line: dict[
            int,
            _CodexToolActivityFact,
        ] = {}
        self._touched_path_scan_count = 0
        self._touched_path_scan_valid = True
        self._touched_paths: list[str] = []
        self._touched_path_set: set[str] = set()

    @staticmethod
    def _decode_call_activity_fact(
        raw_action_name: Any,
        raw_arguments: Any,
    ) -> _CodexToolActivityFact | None:
        normalized_action_name = _normalized_codex_action_name(raw_action_name)
        if normalized_action_name is None:
            return None
        lowered_action_name = normalized_action_name.lower()
        command = None
        if lowered_action_name in {"exec", "exec_command"}:
            command = _normalize_command(
                _codex_command_text(lowered_action_name, raw_arguments)
            )
        return _CodexToolActivityFact(
            action_name=normalized_action_name,
            command=command,
        )

    def record_call(
        self,
        call_id: Any,
        raw_action_name: Any,
        raw_arguments: Any,
        *,
        source_line_number: int,
    ) -> None:
        if len(self._carrier_identities_by_source_line) >= self._carrier_cap:
            return
        validated_call_id = validated_codex_identifier(call_id)
        normalized_action_name = _normalized_codex_action_name(raw_action_name)
        self._carrier_identities_by_source_line[source_line_number] = (
            _CodexToolActivityCarrierIdentity(
                call_id=validated_call_id,
                action_name=normalized_action_name,
            )
        )
        candidate_fact = self._decode_call_activity_fact(
            normalized_action_name,
            raw_arguments,
        )
        if candidate_fact is None:
            return
        if (
            validated_call_id is not None
            and validated_call_id in self._facts_by_call_id
        ):
            existing_fact = self._facts_by_call_id[validated_call_id]
            if existing_fact is None:
                return
            self._facts_by_call_id[validated_call_id] = (
                _merge_codex_tool_activity_facts(
                    existing_fact,
                    candidate_fact,
                )
            )
            return
        if validated_call_id is not None:
            self._facts_by_call_id[validated_call_id] = candidate_fact
        else:
            self._anonymous_call_facts_by_source_line[source_line_number] = (
                candidate_fact
            )

    def expects_carrier_on_source_line(self, source_line_number: int) -> bool:
        return source_line_number in self._carrier_identities_by_source_line

    def _final_fact_for_carrier(
        self,
        source_line_number: int,
        identity: _CodexToolActivityCarrierIdentity,
    ) -> _CodexToolActivityFact | None:
        if identity.call_id is None:
            final_fact = self._anonymous_call_facts_by_source_line.get(
                source_line_number
            )
        else:
            final_fact = self._facts_by_call_id.get(identity.call_id)
        if (
            final_fact is None
            or identity.action_name is None
            or _merge_codex_action_names(
                final_fact.action_name,
                identity.action_name,
            )
            is None
        ):
            return None
        return final_fact

    def needs_touched_path_scan(self) -> bool:
        if self._retained_touched_path_cap <= 0:
            return False
        for source_line_number, identity in (
            self._carrier_identities_by_source_line.items()
        ):
            if (
                identity.action_name is not None
                and identity.action_name.lower() == "apply_patch"
                and self._final_fact_for_carrier(source_line_number, identity)
                is not None
            ):
                return True
        return False

    def record_touched_paths(
        self,
        source_line_number: int,
        carrier: _CodexActionCarrier | None,
        *,
        cwd: str | None,
    ) -> None:
        """Collect paths from one phase-two carrier after final reconciliation."""

        expected_identity = self._carrier_identities_by_source_line.get(
            source_line_number
        )
        if expected_identity is None:
            self._touched_path_scan_valid = False
            return
        self._touched_path_scan_count += 1
        if carrier is None:
            self._touched_path_scan_valid = False
            return
        actual_identity = _CodexToolActivityCarrierIdentity(
            call_id=validated_codex_identifier(carrier.call_id),
            action_name=_normalized_codex_action_name(carrier.action_name),
        )
        if actual_identity != expected_identity:
            self._touched_path_scan_valid = False
            return

        final_fact = self._final_fact_for_carrier(
            source_line_number,
            expected_identity,
        )
        if (
            final_fact is None
            or expected_identity.action_name is None
            or expected_identity.action_name.lower() != "apply_patch"
            or len(self._touched_paths) >= self._retained_touched_path_cap
            or not isinstance(carrier.raw_arguments, str)
            or len(carrier.raw_arguments) > _CODEX_TOOL_ARGUMENT_TEXT_MAX
        ):
            return

        for raw_path in _iter_codex_apply_patch_paths(carrier.raw_arguments):
            normalized_path = _normalize_touched_path(
                _codex_relativize_touched_path(raw_path, cwd)
            )
            if normalized_path is None or normalized_path in self._touched_path_set:
                continue
            self._touched_paths.append(normalized_path)
            self._touched_path_set.add(normalized_path)
            if len(self._touched_paths) >= self._retained_touched_path_cap:
                break

    def finish_touched_path_scan(self, *, source_unchanged: bool) -> None:
        """Keep phase-two paths only when every retained carrier was re-read."""

        if (
            not source_unchanged
            or not self._touched_path_scan_valid
            or self._touched_path_scan_count
            != len(self._carrier_identities_by_source_line)
        ):
            self._touched_paths.clear()
            self._touched_path_set.clear()

    def _retained_facts_in_carrier_order(self) -> Iterator[_CodexToolActivityFact]:
        seen_call_ids: set[str] = set()
        for source_line_number, identity in (
            self._carrier_identities_by_source_line.items()
        ):
            if (
                identity.call_id is not None
                and identity.call_id in seen_call_ids
            ):
                continue
            fact = self._final_fact_for_carrier(source_line_number, identity)
            if fact is None:
                continue
            if identity.call_id is not None:
                seen_call_ids.add(identity.call_id)
            yield fact

    def as_activity(self) -> dict[str, Any]:
        action_name_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        commands: list[str] = []
        command_set: set[str] = set()
        for fact in self._retained_facts_in_carrier_order():
            action_name_counts[fact.action_name] = (
                action_name_counts.get(fact.action_name, 0) + 1
            )
            category = tool_category(fact.action_name)
            category_counts[category] = category_counts.get(category, 0) + 1
            if (
                fact.command
                and len(commands) < _COMMANDS_PER_BATCH_MAX
                and fact.command not in command_set
            ):
                commands.append(fact.command)
                command_set.add(fact.command)

        activity: dict[str, Any] = {}
        if category_counts:
            activity["tool_category_counts"] = dict(
                sorted(category_counts.items())
            )
        if action_name_counts:
            activity["tool_names"] = [
                {"name": action_name, "count": count}
                for action_name, count in sorted(action_name_counts.items())
            ]
        if self._touched_paths:
            activity["touched_files"] = list(self._touched_paths)
        if commands:
            activity["commands"] = commands
        return activity


def _codex_open_file_snapshot(handle: TextIO) -> tuple[int, int, int, int, int]:
    """Identity and mutation fields for one already-open regular rollout."""

    file_stat = os.fstat(handle.fileno())
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
    )


def _collect_codex_rollout_touched_paths(
    handle: TextIO,
    *,
    source_snapshot: tuple[int, int, int, int, int] | None,
    source_line_count: int,
    tool_activity: _CodexToolActivityAccumulator,
    cwd: str | None,
) -> None:
    """Re-read retained carrier lines and fail closed if the source changes."""

    if not tool_activity.needs_touched_path_scan():
        return
    source_unchanged = False
    try:
        if (
            source_snapshot is None
            or source_snapshot != _codex_open_file_snapshot(handle)
        ):
            raise OSError("rollout changed during its primary scan")
        handle.seek(0)
        for source_line_number in range(1, source_line_count + 1):
            line = handle.readline()
            if line == "":
                break
            if not tool_activity.expects_carrier_on_source_line(
                source_line_number
            ):
                continue
            carrier = None
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                obj = None
            if isinstance(obj, Mapping):
                payload = obj.get("payload")
                if isinstance(payload, Mapping):
                    carrier = _codex_action_carrier(payload)
            tool_activity.record_touched_paths(
                source_line_number,
                carrier,
                cwd=cwd,
            )
        else:
            source_unchanged = source_snapshot == _codex_open_file_snapshot(handle)
    except (OSError, ValueError):
        # Touched files are optional Actions metadata. If the live rollout moves
        # while it is being scanned, omit paths instead of mixing two snapshots.
        source_unchanged = False
    tool_activity.finish_touched_path_scan(source_unchanged=source_unchanged)


def _read_codex_rollout_usage(
    source: _RegularSourceFile | Path,
    *,
    _parse_stats: dict[str, int] | None = None,
    _observation_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Cache-wrapped codex rollout reader. On a hit the file's bytes and the
    parser code are proven identical, so the returned value (deep-copied so the
    caller's later lineage normalization cannot mutate the cache) equals a fresh
    parse. Out-params ``_parse_stats`` / ``_observation_metadata`` are restored
    from the memo so downstream behavior is unchanged."""

    cache = _CODEX_PARSE_CACHE.get()
    source_file = _coerce_private_regular_source(source) if cache is not None else None
    if cache is None or source_file is None:
        return _read_codex_rollout_usage_uncached(
            source, _parse_stats=_parse_stats, _observation_metadata=_observation_metadata
        )
    key = (
        str(source_file.path),
        int(source_file.mtime_ns),
        int(source_file.size),
        int(source_file.inode),
    )
    cached = cache.get(key)
    if cached is not None:
        # Stage every fallible step (unpack, deepcopy, int-convert) into locals
        # BEFORE touching the caller's out-params. A bad entry then leaves
        # ``_parse_stats``/``_observation_metadata`` pristine and we fall through
        # to a clean re-parse — never a partial mutation that the MISS path would
        # then double-apply.
        staged: tuple[Any, dict[str, Any], dict[str, int]] | None = None
        try:
            result, cached_obs, cached_stats = cached
            result_copy = copy.deepcopy(result)
            obs_copy = copy.deepcopy(cached_obs) if cached_obs else {}
            stat_deltas = {str(k): int(v) for k, v in dict(cached_stats or {}).items()}
            staged = (result_copy, obs_copy, stat_deltas)
        except Exception:  # noqa: BLE001 - a bad entry must never poison a scan.
            staged = None
        if staged is not None:
            result_copy, obs_copy, stat_deltas = staged
            # These mutations are infallible (plain dict.update / int add), so
            # once we start applying them the caller state stays consistent.
            if _observation_metadata is not None and obs_copy:
                _observation_metadata.update(obs_copy)
            if _parse_stats is not None and stat_deltas:
                for stat_key, delta in stat_deltas.items():
                    _parse_stats[stat_key] = _safe_nonnegative_int(_parse_stats.get(stat_key)) + delta
            return result_copy
    local_stats: dict[str, int] = {}
    local_obs: dict[str, Any] = {}
    result = _read_codex_rollout_usage_uncached(
        source, _parse_stats=local_stats, _observation_metadata=local_obs
    )
    try:
        cache.put(key, (copy.deepcopy(result), copy.deepcopy(local_obs), dict(local_stats)))
    except Exception:  # noqa: BLE001
        pass
    if _observation_metadata is not None:
        _observation_metadata.update(local_obs)
    if _parse_stats is not None:
        for stat_key, delta in local_stats.items():
            _parse_stats[stat_key] = _safe_nonnegative_int(_parse_stats.get(stat_key)) + int(delta)
    return result


def _read_codex_rollout_usage_uncached(
    source: _RegularSourceFile | Path,
    *,
    _parse_stats: dict[str, int] | None = None,
    _observation_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read token totals, session lineage, Actions, and client-log evidence.

    Returns None only when the rollout holds neither a total_token_usage line
    nor any client-log evidence; an evidence-only rollout returns a dict
    WITHOUT ``input_tokens`` so the caller's sqlite tokens_used fallback still
    fires while the evidence rides along. The primary scan reconciles call
    identities; a bounded second scan extracts paths only from final valid calls.
    """

    source_file = _coerce_private_regular_source(source)
    if source_file is None:
        return None
    latest: dict[str, Any] | None = None
    turn_count = 0
    model: str | None = None
    session_meta_parent: str | None = None
    session_meta_id: str | None = None
    session_meta_cwd: str | None = None
    session_meta_seen = False
    replay_source_detected = False
    spawn_source_detected = False
    replayed_parent_session_meta = False
    first_activity_at: int | None = None
    last_activity_at: int | None = None
    # Discovery-side Actions retain only bounded identities and scrubbed commands.
    # Patch bodies are revisited, never retained, after all duplicate call
    # representations have been reconciled.
    tool_activity = _CodexToolActivityAccumulator()
    source_line_count = 0
    token_usage_events: list[tuple[Any, ...]] = []
    raw_token_event_count = 0
    replay_prefix_token_events = 0
    replay_baseline: dict[str, Any] | None = None
    previous_total_usage: dict[str, Any] | None = None
    replay_probe_event: tuple[Any, ...] | None = None
    replay_second: str | None = None
    replay_probe_complete = False
    # Client-log evidence is decoded into carrier-independent fragments during
    # the scan and reconciled by logical call id after it. This supports legacy
    # function/output pairs, legacy mcp_tool_call_end records, and current
    # paginated item_completed/McpToolCall records without making file order or
    # one Codex persistence representation part of the evidence model.
    evidence_fragments: list[CodexEvidenceFragment] = []
    evidence_fragment_cap_exceeded = False
    evidence = LogEvidenceAccumulator()
    saw_nonempty_line = False
    saw_valid_object = False
    saw_malformed_line = False
    saw_token_usage_schema_drift = False
    cache_write_tokens_applicable = False

    def _record_tool_activity(
        source_line_number: int,
        carrier: _CodexActionCarrier,
    ) -> None:
        tool_activity.record_call(
            carrier.call_id,
            carrier.action_name,
            carrier.raw_arguments,
            source_line_number=source_line_number,
        )
    with _open_regular_source_text(source_file) as handle:
        try:
            source_snapshot = _codex_open_file_snapshot(handle)
        except OSError:
            source_snapshot = None
        for source_line_count, line in enumerate(handle, start=1):
            if line.strip():
                saw_nonempty_line = True
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                if line.strip():
                    saw_malformed_line = True
                continue
            if not isinstance(obj, dict):
                # A rollout line can hold VALID non-dict JSON (e.g. a torn
                # partial write leaving a bare number/string/array). One such
                # line must never crash the whole usage import.
                if line.strip():
                    saw_malformed_line = True
                continue
            saw_valid_object = True
            timestamp = _timestamp_seconds(obj.get("timestamp"))
            if timestamp is not None:
                first_activity_at = min(
                    first_activity_at or timestamp,
                    timestamp,
                )
                last_activity_at = max(last_activity_at or timestamp, timestamp)
            if obj.get("type") == "session_meta" and not session_meta_seen:
                # Codex records the spawning thread's id verbatim on the
                # rollout's OWN (first) session_meta line; read it as-is —
                # never inferred. ONLY that first meta may donate a parent:
                # resumed/compacted rollouts re-emit later session_meta lines
                # (8 of 26 real rollouts have several) that can describe
                # OTHER threads, and adopting one would nest this session
                # under an unrelated root. A self-pointer (parent == the
                # thread's own id) is corrupt linkage and is dropped
                # (missing beats wrong).
                session_meta_seen = True
                meta_payload = obj.get("payload")
                if isinstance(meta_payload, dict):
                    replay_source_detected = _codex_session_meta_has_replay_source(
                        meta_payload
                    )
                    spawn_source_detected = _codex_session_meta_has_spawn_source(
                        meta_payload
                    )
                    own_id_value = meta_payload.get("id")
                    if isinstance(own_id_value, str) and own_id_value:
                        session_meta_id = own_id_value
                    cwd_value = meta_payload.get("cwd")
                    if isinstance(cwd_value, str) and cwd_value:
                        session_meta_cwd = cwd_value
                    parent = meta_payload.get("parent_thread_id")
                    own_id = meta_payload.get("id")
                    if isinstance(parent, str) and parent and parent != own_id:
                        session_meta_parent = parent
            elif obj.get("type") == "session_meta":
                replay_payload = obj.get("payload")
                if (
                    isinstance(replay_payload, dict)
                    and session_meta_parent
                    and replay_payload.get("id") == session_meta_parent
                ):
                    replayed_parent_session_meta = True
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")
            action_carrier = _codex_action_carrier(payload)
            if action_carrier is not None:
                _record_tool_activity(source_line_count, action_carrier)

            if len(evidence_fragments) < _CODEX_EVIDENCE_FRAGMENT_CAP:
                fragment = decode_codex_evidence_fragment(obj)
                if fragment is not None:
                    evidence_fragments.append(fragment)
            elif (
                not evidence_fragment_cap_exceeded
                and codex_record_may_contain_evidence(obj)
            ):
                # Reconciliation must retain out-of-order descriptors and
                # outputs, but it must not accumulate or repeatedly decode an
                # unbounded rollout. Make truncation an explicit evidence-health
                # failure and one bounded skip instead of claiming completeness.
                evidence_fragment_cap_exceeded = True

            carrier_model = codex_model_from_record(obj)
            if model is None and carrier_model is not None:
                model = carrier_model
            info = payload.get("info")
            if isinstance(info, dict) and "total_token_usage" in info:
                total_token_usage = info.get("total_token_usage")
                if not isinstance(total_token_usage, dict):
                    # A present carrier with the wrong shape is schema drift,
                    # not absence. Preserve the session as incomplete and never
                    # demote it to the lower-precedence sqlite tokens_used row.
                    saw_token_usage_schema_drift = True
                    continue
                latest = dict(total_token_usage)
                raw_token_event_count += 1
                if "cache_write_tokens" in total_token_usage:
                    cache_write_tokens_applicable = True
                event_timestamp = (
                    obj.get("timestamp")
                    if isinstance(obj.get("timestamp"), str)
                    else None
                )
                current_total_usage = dict(total_token_usage)
                raw_last_token_usage = info.get("last_token_usage")
                last_token_usage = (
                    dict(raw_last_token_usage)
                    if isinstance(raw_last_token_usage, dict)
                    else None
                )
                if isinstance(last_token_usage, dict) and (
                    "cache_write_tokens" in last_token_usage
                ):
                    cache_write_tokens_applicable = True
                if "last_token_usage" in info and (
                    last_token_usage is None
                    or _codex_last_token_usage_schema_drift(
                        current_value=current_total_usage,
                        last_value=last_token_usage,
                    )
                ):
                    # A malformed or impossible per-turn counter is schema
                    # drift, never a value to add. Keep the current cumulative
                    # compatibility row visible, but make the session
                    # incomplete so import planning cannot persist it.
                    saw_token_usage_schema_drift = True
                    last_token_usage = None
                token_event = (
                    event_timestamp,
                    carrier_model,
                    _codex_token_delta(
                        current_value=current_total_usage,
                        last_value=last_token_usage,
                        previous_value=previous_total_usage,
                    ),
                    _codex_retained_delta_start_proven(
                        current=current_total_usage,
                        last=last_token_usage,
                        previous=previous_total_usage,
                    ),
                    _codex_token_delta_presence(
                        current_value=current_total_usage,
                        last_value=last_token_usage,
                        previous_value=previous_total_usage,
                    ),
                )
                event_second = _codex_timestamp_second(event_timestamp)
                if (
                    replay_source_detected or replayed_parent_session_meta
                ) and not replay_probe_complete:
                    if replay_second is not None:
                        if event_second == replay_second:
                            replay_prefix_token_events += 1
                            replay_baseline = dict(info["total_token_usage"])
                        else:
                            replay_probe_complete = True
                            token_usage_events.append(token_event)
                    elif replay_probe_event is None:
                        # ccusage v20.0.14 treats a child prefix as replay only
                        # after TWO token events share its first timestamp
                        # second. Buffer one event so a genuine fast first turn
                        # is not discarded merely because it shares session
                        # creation's timestamp.
                        replay_probe_event = token_event
                    else:
                        first_second = _codex_timestamp_second(
                            replay_probe_event[0]
                        )
                        if first_second is not None and first_second == event_second:
                            replay_second = first_second
                            replay_prefix_token_events = 2
                            replay_baseline = current_total_usage
                            replay_probe_event = None
                        else:
                            token_usage_events.extend(
                                (replay_probe_event, token_event)
                            )
                            replay_probe_event = None
                            replay_probe_complete = True
                else:
                    token_usage_events.append(
                        token_event
                    )
                previous_total_usage = current_total_usage
                turn_count += 1
        _collect_codex_rollout_touched_paths(
            handle,
            source_snapshot=source_snapshot,
            source_line_count=source_line_count,
            tool_activity=tool_activity,
            cwd=session_meta_cwd,
        )
    if evidence_fragment_cap_exceeded:
        # A later fragment could contradict a retained call id. Without the
        # complete bounded set, no partial donor is authoritative.
        evidence.record_skip()
        evidence_parse_incomplete = True
    else:
        evidence_parse_incomplete = reconcile_codex_evidence_fragments(
            evidence_fragments,
            evidence,
        )
    if replay_probe_event is not None:
        token_usage_events.append(replay_probe_event)
    if (
        saw_nonempty_line
        and (not saw_valid_object or saw_malformed_line)
        and _parse_stats is not None
    ):
        _parse_stats["unparseable_rollouts"] = _safe_nonnegative_int(_parse_stats.get("unparseable_rollouts")) + 1
    if saw_token_usage_schema_drift and _parse_stats is not None:
        _parse_stats["schema_drift_rollouts"] = _safe_nonnegative_int(
            _parse_stats.get("schema_drift_rollouts")
        ) + 1
    if evidence_parse_incomplete and _parse_stats is not None:
        _parse_stats["evidence_schema_drift_rollouts"] = _safe_nonnegative_int(
            _parse_stats.get("evidence_schema_drift_rollouts")
        ) + 1
    rollout_tool_activity = tool_activity.as_activity()
    if _observation_metadata is not None:
        _observation_metadata.update(
            {
                "session_id": session_meta_id,
                "parent_session_id": session_meta_parent,
                "cwd": session_meta_cwd,
                "first_activity_at": first_activity_at,
                "last_activity_at": last_activity_at,
                "observed_models": [model] if model else [],
                "valid_object_count": int(saw_valid_object),
                "tool_activity": rollout_tool_activity,
                **evidence.as_usage_fields(),
            }
        )
    if latest is None:
        if not evidence.has_evidence and not saw_token_usage_schema_drift:
            return None
        result: dict[str, Any] = evidence.as_usage_fields()
        if saw_token_usage_schema_drift:
            result["_token_usage_schema_drift"] = True
        if spawn_source_detected:
            result["_spawn_source_detected"] = True
        if session_meta_parent:
            result["session_meta_parent_thread_id"] = session_meta_parent
        return result
    for counter_name in (*_CODEX_TOKEN_COUNTER_FIELDS, "total_tokens"):
        latest[f"_{counter_name}_reported"] = _codex_counter_source_present(
            latest,
            counter_name,
        )
    latest["_cache_write_tokens_applicable"] = cache_write_tokens_applicable
    latest["turn_count"] = turn_count
    latest["_token_usage_events"] = token_usage_events
    latest["_raw_token_event_count"] = raw_token_event_count
    latest["_replay_prefix_token_events"] = replay_prefix_token_events
    latest["_replay_source_detected"] = replay_source_detected
    latest["_spawn_source_detected"] = spawn_source_detected
    latest["_replayed_parent_session_meta"] = replayed_parent_session_meta
    if saw_token_usage_schema_drift:
        latest["_token_usage_schema_drift"] = True
    if replay_baseline is not None:
        latest["_replay_baseline"] = replay_baseline
    if token_usage_events:
        latest["_retained_delta_start_proven"] = bool(
            token_usage_events[0][3]
        )
    if model:
        latest["model"] = model
    if session_meta_parent:
        latest["session_meta_parent_thread_id"] = session_meta_parent
    if evidence.has_evidence:
        latest.update(evidence.as_usage_fields())
    return latest


def _read_claude_project_usage(path: Path) -> dict[str, Any] | None:
    usages = _read_claude_project_usages(path)
    return usages[0] if usages else None


def _read_claude_project_usages(
    path: Path,
    *,
    seen_usage_outputs: dict[str, int] | None = None,
    _parse_stats: dict[str, int] | None = None,
    _observation_metadata: dict[str, Any] | None = None,
    projects_root: Path | None = None,
    projects_root_fd: int | None = None,
    expected_fingerprint: _ClaudeFileFingerprint | None = None,
) -> list[dict[str, Any]]:
    totals_by_model: dict[str, dict[str, int]] = {}
    model_order: list[str] = []
    session_id: str | None = None
    cwd: str | None = None
    custom_title: str | None = None
    ai_title: str | None = None
    first_activity_at: int | None = None
    last_activity_at: int | None = None
    observed_models: list[str] = []
    # Client-log evidence (log_evidence.py), per FILE: subagent transcripts are
    # separate rows with ':stem'-suffixed session keys, so evidence recorded
    # inside a subagent transcript rides the subagent's own row. Pairing:
    # assistant tool_use blocks named mcp__<server>__<creation tool> (the
    # agentacct and pre-rename "agent-chronicle"/"agent-sentinel" server keys
    # are accepted)
    # remembered by block id, then the in-message tool_result blocks for those
    # ids run the shared strict shape check. The line-level toolUseResult
    # mirror is deliberately NOT read (one canonical location); free-text
    # event-id echoes (e.g. Task-prompt echoes) are structurally invisible.
    accepted_tool_ids: dict[str, str] = {}
    rejected_tool_ids: set[str] = set()
    evidence = LogEvidenceAccumulator()
    saw_nonempty_line = False
    saw_valid_object = False
    malformed_lines = 0
    invalid_usage_rows = 0
    # Cross-file replay dedup is TRANSACTIONAL: this file's dedup keys are staged
    # locally and merged into the shared ``seen_usage_outputs`` only after the
    # end-fingerprint check confirms the file was not rewritten mid-read. So a file
    # that raises (e.g. changed_during_scan) leaves NO ghost keys behind to
    # suppress a sibling's replayed rows. Within a file, lookups check the stage
    # first so same-file replays still dedup exactly as before.
    staged_usage_outputs: dict[str, int] = {}
    file_fd, file_stat = _open_claude_transcript_fd(
        path,
        projects_root=projects_root or path.parent,
        projects_root_fd=projects_root_fd,
    )
    fingerprint = _claude_file_fingerprint(file_stat)
    if expected_fingerprint is not None and not _claude_fingerprint_matches(
        expected_fingerprint,
        fingerprint,
    ):
        os.close(file_fd)
        raise _ClaudeTranscriptChangedDuringScanError(
            "claude transcript changed before usage read"
        )
    with os.fdopen(file_fd, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                saw_nonempty_line = True
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                if line.strip():
                    malformed_lines += 1
                continue
            if not isinstance(obj, dict):
                continue
            saw_valid_object = True
            timestamp = _timestamp_seconds(obj.get("timestamp"))
            message_for_timestamp = obj.get("message")
            if timestamp is None and isinstance(message_for_timestamp, dict):
                timestamp = _timestamp_seconds(message_for_timestamp.get("timestamp"))
            if timestamp is not None:
                first_activity_at = min(first_activity_at or timestamp, timestamp)
                last_activity_at = max(last_activity_at or timestamp, timestamp)
            if session_id is None and obj.get("sessionId"):
                session_id = str(obj.get("sessionId"))
            if cwd is None and obj.get("cwd"):
                cwd = str(obj.get("cwd"))
            # Claude Code writes explicit session titles as top-level
            # transcript fields. customTitle is user-controlled and always
            # wins over aiTitle regardless of line order. Deliberately do not
            # inspect user message content or synthesize a title from prompts.
            if isinstance(obj.get("customTitle"), str):
                custom_title = obj["customTitle"]
            if isinstance(obj.get("aiTitle"), str):
                ai_title = obj["aiTitle"]
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            observed_model = _limited_optional_text(message.get("model"), 120)
            if observed_model is not None and observed_model not in observed_models:
                observed_models.append(observed_model)
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "tool_use":
                        block_id = block.get("id")
                        if isinstance(block_id, str) and block_id:
                            verdict = classify_claude_tool_use(block.get("name"))
                            if verdict == "accepted":
                                accepted_tool_ids[block_id] = str(block.get("name"))
                            elif verdict == "rejected":
                                rejected_tool_ids.add(block_id)
                    elif block_type == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        if isinstance(tool_use_id, str) and tool_use_id in accepted_tool_ids:
                            # A refused call's tool_result carries the server's
                            # error text here; the accumulator classifies it
                            # into the bounded refusal vocabulary and keeps no
                            # part of the message.
                            evidence.add_output_text(
                                _claude_tool_result_text(block.get("content")),
                                tool=claude_tool_use_creation_tool(accepted_tool_ids[tool_use_id]),
                            )
                        elif isinstance(tool_use_id, str) and tool_use_id in rejected_tool_ids:
                            evidence.record_skip()
            usage_present = "usage" in message
            usage = message.get("usage")
            is_assistant_message = (
                obj.get("type") == "assistant"
                or message.get("role") == "assistant"
            )
            if not isinstance(usage, dict):
                if is_assistant_message and usage_present:
                    invalid_usage_rows += 1
                continue
            token_keys = (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
            if is_assistant_message and (
                not any(key in usage for key in token_keys)
                or any(
                    not _is_nonnegative_usage_token_value(usage.get(key))
                    for key in token_keys
                    if key in usage
                )
            ):
                invalid_usage_rows += 1
                continue
            input_tokens = _safe_nonnegative_int(usage.get("input_tokens"))
            output_tokens = _safe_nonnegative_int(usage.get("output_tokens"))
            cache_creation_input_tokens = _safe_nonnegative_int(usage.get("cache_creation_input_tokens"))
            cache_read_input_tokens = _safe_nonnegative_int(usage.get("cache_read_input_tokens"))
            if input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens <= 0:
                continue
            model = message.get("model") if isinstance(message.get("model"), str) else "unknown"
            if model not in totals_by_model:
                totals_by_model[model] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_creation_5m_input_tokens": 0,
                    "cache_creation_1h_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "turn_count": 0,
                    "raw_usage_rows": 0,
                    "deduplicated_usage_rows": 0,
                    "started_at": 0,
                    "updated_at": 0,
                }
                model_order.append(model)
            totals = totals_by_model[model]
            totals["raw_usage_rows"] += 1
            usage_key = _claude_usage_dedupe_key(path, obj, message, usage)
            if usage_key and seen_usage_outputs is not None:
                # Dedup against prior files (committed, shared) AND this file's own
                # earlier rows (staged), but WRITE only to the stage — the shared
                # dict is updated after a clean end-fingerprint (see below).
                previous_output = staged_usage_outputs.get(usage_key)
                if previous_output is None:
                    previous_output = seen_usage_outputs.get(usage_key)
                if previous_output is not None:
                    totals["deduplicated_usage_rows"] += 1
                    output_tokens = max(0, output_tokens - previous_output)
                    if output_tokens <= 0:
                        continue
                    input_tokens = 0
                    cache_creation_input_tokens = 0
                    cache_read_input_tokens = 0
                staged_usage_outputs[usage_key] = max(previous_output or 0, _safe_nonnegative_int(usage.get("output_tokens")))
            timestamp = _timestamp_seconds(obj.get("timestamp") or message.get("timestamp"))
            if timestamp is not None:
                totals["started_at"] = min(totals["started_at"] or timestamp, timestamp)
                totals["updated_at"] = max(totals["updated_at"], timestamp)
            cache_creation = usage.get("cache_creation")
            cache_creation_5m_input_tokens = 0
            cache_creation_1h_input_tokens = 0
            if cache_creation_input_tokens > 0 and isinstance(cache_creation, dict):
                cache_creation_5m_input_tokens = _safe_nonnegative_int(cache_creation.get("ephemeral_5m_input_tokens"))
                cache_creation_1h_input_tokens = _safe_nonnegative_int(cache_creation.get("ephemeral_1h_input_tokens"))
            totals["input_tokens"] += input_tokens
            totals["output_tokens"] += output_tokens
            totals["cache_creation_input_tokens"] += cache_creation_input_tokens
            totals["cache_creation_5m_input_tokens"] += cache_creation_5m_input_tokens
            totals["cache_creation_1h_input_tokens"] += cache_creation_1h_input_tokens
            totals["cache_read_input_tokens"] += cache_read_input_tokens
            totals["turn_count"] += 1
        end_fingerprint = _claude_file_fingerprint(os.fstat(handle.fileno()))
    if end_fingerprint != fingerprint:
        raise _ClaudeTranscriptChangedDuringScanError(
            "claude transcript changed during usage read"
        )
    # The file read cleanly (no mid-read rewrite) — commit its staged dedup keys
    # into the shared map so later files in the cohort dedup replays against them.
    if seen_usage_outputs is not None:
        for _key, _output in staged_usage_outputs.items():
            seen_usage_outputs[_key] = max(seen_usage_outputs.get(_key, 0), _output)
    if saw_nonempty_line and not saw_valid_object and _parse_stats is not None:
        _parse_stats["unparseable_transcripts"] = (
            _safe_nonnegative_int(_parse_stats.get("unparseable_transcripts")) + 1
        )
    # An entirely unreadable transcript already contributes the single,
    # stronger ``unparseable`` diagnostic above. Surface malformed-line
    # drift separately only for mixed files where a real object was usable;
    # otherwise one bad file would be counted twice.
    if malformed_lines and saw_valid_object and _parse_stats is not None:
        _parse_stats["malformed_transcript_lines"] = (
            _safe_nonnegative_int(_parse_stats.get("malformed_transcript_lines"))
            + malformed_lines
        )
    if invalid_usage_rows and _parse_stats is not None:
        _parse_stats["invalid_usage_rows"] = (
            _safe_nonnegative_int(_parse_stats.get("invalid_usage_rows"))
            + invalid_usage_rows
        )
    if _observation_metadata is not None:
        _observation_metadata.update(
            {
                "session_id": session_id or path.stem,
                "cwd": cwd,
                "title": _sanitized_session_title(custom_title)
                or _sanitized_session_title(ai_title),
                "first_activity_at": first_activity_at,
                "last_activity_at": last_activity_at,
                "observed_models": observed_models,
                "saw_valid_object": saw_valid_object,
                **evidence.as_usage_fields(),
            }
        )
    usages = []
    for model in model_order:
        totals = totals_by_model[model]
        if totals["turn_count"] <= 0:
            continue
        usages.append(
            {
                **totals,
                "session_id": session_id or path.stem,
                "cwd": cwd,
                "title": _sanitized_session_title(custom_title) or _sanitized_session_title(ai_title),
                "model": None if model == "unknown" else model,
                "started_at": totals["started_at"] or None,
                "updated_at": totals["updated_at"] or None,
            }
        )
    if usages and evidence.has_evidence:
        # The SAME evidence triple rides every per-model lane row of this
        # file; the read-time index dedups donors by (client, base session),
        # so lanes never double-count as multiple donors.
        for usage_row in usages:
            usage_row.update(evidence.as_usage_fields())
    return usages


def _claude_tool_result_text(content: Any) -> str | None:
    """First text block of an in-message tool_result content list."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return None


def _claude_usage_dedupe_key(path: Path, obj: dict[str, Any], message: dict[str, Any], usage: dict[str, Any]) -> str | None:
    """Identify Claude Code assistant usage rows that are replayed in sidechain files.

    Claude Code can copy parent assistant messages into subagent/sidechain
    transcript files. Those copied rows carry the same message id/request id and
    the same usage object, so counting every file overstates token usage.
    """

    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        return f"message:{message_id}"
    request_id = obj.get("requestId") or obj.get("request_id")
    if isinstance(request_id, str) and request_id:
        return f"request:{request_id}"
    row_uuid = obj.get("uuid")
    if isinstance(row_uuid, str) and row_uuid:
        return f"row:{row_uuid}"
    model = message.get("model") if isinstance(message.get("model"), str) else "unknown"
    timestamp = obj.get("timestamp")
    usage_tuple = (
        _safe_nonnegative_int(usage.get("input_tokens")),
        _safe_nonnegative_int(usage.get("cache_creation_input_tokens")),
        _safe_nonnegative_int(usage.get("cache_read_input_tokens")),
        _safe_nonnegative_int(usage.get("output_tokens")),
    )
    return f"file:{path}:{timestamp}:{model}:{usage_tuple}"


def _matching_regular_source_files(
    root: Path,
    *,
    patterns: tuple[str, ...],
) -> list[_RegularSourceFile]:
    """Return newest regular descendants while rejecting symlink roots/files."""

    try:
        root_stat = root.lstat()
    except OSError:
        return []
    if not stat.S_ISDIR(root_stat.st_mode):
        return []
    candidates: dict[Path, _RegularSourceFile] = {}
    for pattern in patterns:
        try:
            paths = root.rglob(pattern)
            for path in paths:
                source = _regular_source_file(path, root=root)
                if source is not None:
                    candidates[source.path] = source
        except OSError:
            continue
    return sorted(
        candidates.values(),
        key=lambda source: source.mtime,
        reverse=True,
    )


def _read_opencode_json_usage(
    source: _RegularSourceFile,
) -> dict[str, Any] | None:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens_reported": False,
        "cache_write_tokens_reported": False,
        "reasoning_output_tokens": 0,
        "turn_count": 0,
    }
    session_id: str | None = None
    model: str | None = None
    cost = 0.0
    saw_cost = False
    with _open_regular_source_text(source) as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Export directories can contain valid JSON that is not an OpenCode
            # event record, such as a JSON array. Ignore it so one stale file
            # cannot abort discovery for every client during onboarding.
            if not isinstance(obj, dict):
                continue
            if session_id is None and obj.get("sessionID"):
                session_id = str(obj.get("sessionID"))
            if model is None:
                model = _find_first_string_key(obj, "model")
            part = obj.get("part")
            if not isinstance(part, dict) or part.get("type") != "step-finish":
                continue
            tokens = part.get("tokens")
            if not isinstance(tokens, dict):
                continue
            totals["input_tokens"] += _safe_nonnegative_int(tokens.get("input"))
            totals["output_tokens"] += _safe_nonnegative_int(tokens.get("output"))
            totals["reasoning_output_tokens"] += _safe_nonnegative_int(tokens.get("reasoning"))
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                cache_read = _safe_nonnegative_int(cache.get("read"))
                cache_write = _safe_nonnegative_int(cache.get("write"))
                totals["cache_read_tokens"] += cache_read
                totals["cache_write_tokens"] += cache_write
                totals["cached_input_tokens"] += cache_read + cache_write
                if "read" in cache:
                    totals["cache_read_tokens_reported"] = True
                if "write" in cache:
                    totals["cache_write_tokens_reported"] = True
            maybe_cost = _optional_float(part.get("cost"))
            if maybe_cost is not None:
                cost += maybe_cost
                saw_cost = True
            totals["turn_count"] += 1
    if totals["turn_count"] <= 0:
        return None
    return {
        **totals,
        "session_id": session_id or source.path.stem,
        "model": model,
        "cost_usd": cost if saw_cost else None,
    }


def _openclaw_session_paths(
    openclaw_home: Path | None,
) -> list[_RegularSourceFile]:
    if openclaw_home is not None:
        roots = [openclaw_home.expanduser()]
    else:
        env_value = _env_text("OPENCLAW_DIR")
        roots = [Path(value).expanduser() for value in env_value.split(",") if value.strip()] if env_value else [
            Path.home() / ".openclaw",
            Path.home() / ".clawdbot",
            Path.home() / ".moltbot",
            Path.home() / ".moldbot",
        ]
    sources: list[_RegularSourceFile] = []
    for root in roots:
        sources.extend(
            source
            for source in _matching_regular_source_files(
                root,
                patterns=("*.jsonl*",),
            )
            if _is_openclaw_session_file(source.path.name)
        )
    deduped = {source.path: source for source in sources}
    return sorted(
        deduped.values(),
        key=lambda source: source.mtime,
        reverse=True,
    )


def _is_openclaw_session_file(name: str) -> bool:
    index = name.find(".jsonl")
    if index < 0:
        return False
    suffix = name[index:]
    return suffix == ".jsonl" or suffix.startswith(".jsonl.deleted.") or suffix.startswith(".jsonl.reset.")


def _openclaw_session_id(path: Path) -> str:
    name = path.name
    index = name.find(".jsonl")
    if index < 1:
        return path.stem
    return name[:index]


def _read_openclaw_jsonl_usage(
    source: _RegularSourceFile,
) -> dict[str, Any] | None:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens_reported": False,
        "cache_write_tokens_reported": False,
        "reasoning_tokens": 0,
        "turn_count": 0,
    }
    current_model: str | None = None
    current_provider: str | None = None
    first_started_at: int | None = None
    cost = 0.0
    saw_cost = False
    seen_records: set[tuple[object, ...]] = set()
    with _open_regular_source_text(source) as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _is_openclaw_model_change(obj):
                data = obj.get("data")
                model_source = data if isinstance(data, dict) else obj
                current_model = _limited_optional_text(model_source.get("modelId") or model_source.get("model") or current_model, 120)
                current_provider = _limited_optional_text(model_source.get("provider") or current_provider, 80)
                continue
            message = obj.get("message")
            if obj.get("type") != "message" or not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            input_tokens = _safe_nonnegative_int(usage.get("input"))
            output_tokens = _safe_nonnegative_int(usage.get("output"))
            cache_read = _safe_nonnegative_int(usage.get("cacheRead"))
            cache_write = _safe_nonnegative_int(usage.get("cacheWrite"))
            total_tokens = _safe_nonnegative_int(usage.get("totalTokens"))
            if input_tokens + output_tokens + cache_read + cache_write == 0 and total_tokens > 0:
                output_tokens = total_tokens
            if input_tokens + output_tokens + cache_read + cache_write <= 0:
                continue
            model = _limited_optional_text(message.get("modelId") or message.get("model") or current_model, 120)
            provider = _limited_optional_text(message.get("provider") or current_provider, 80)
            timestamp = _openclaw_timestamp_seconds(message.get("timestamp") or obj.get("timestamp"))
            record_id = (timestamp, model, provider, input_tokens, output_tokens, cache_read, cache_write, total_tokens, _openclaw_cost_total(usage))
            if record_id in seen_records:
                continue
            seen_records.add(record_id)
            totals["input_tokens"] += input_tokens
            totals["output_tokens"] += output_tokens
            totals["cache_read_tokens"] += cache_read
            totals["cache_write_tokens"] += cache_write
            if "cacheRead" in usage:
                totals["cache_read_tokens_reported"] = True
            if "cacheWrite" in usage:
                totals["cache_write_tokens_reported"] = True
            totals["turn_count"] += 1
            current_model = model or current_model
            current_provider = provider or current_provider
            if timestamp is not None:
                first_started_at = timestamp if first_started_at is None else min(first_started_at, timestamp)
            maybe_cost = _openclaw_cost_total(usage)
            if maybe_cost is not None and maybe_cost > 0:
                cost += maybe_cost
                saw_cost = True
    if totals["turn_count"] <= 0:
        return None
    return {
        **totals,
        "session_id": _openclaw_session_id(source.path),
        "model": current_model,
        "provider": current_provider or "openclaw",
        "started_at": first_started_at,
        "cost_usd": cost if saw_cost else None,
    }


def _is_openclaw_model_change(obj: dict[str, Any]) -> bool:
    return obj.get("type") == "model_change" or (obj.get("type") == "custom" and obj.get("customType") == "model-snapshot")


def _openclaw_cost_total(usage: dict[str, Any]) -> float | None:
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        return None
    maybe_total = _optional_float(cost.get("total"))
    if maybe_total is None or maybe_total < 0:
        return None
    return maybe_total


def _openclaw_timestamp_seconds(value: Any) -> int | None:
    if isinstance(value, str):
        from datetime import datetime

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(parsed.timestamp())
    return _timestamp_seconds(value)


def _hermes_state_db_plan(
    hermes_home: Path | None,
) -> _HermesStateDbPlan:
    if hermes_home is not None:
        candidates = [hermes_home.expanduser()]
    else:
        env_value = _env_text("HERMES_HOME")
        candidates = (
            [
                Path(value.strip()).expanduser()
                for value in env_value.split(",")
                if value.strip()
            ]
            if env_value
            else [Path.home() / ".hermes"]
        )
    # Same-inode aliases represent one database. Pick the same lexical home
    # regardless of HERMES_HOME ordering so its namespace fingerprint cannot
    # flip merely because the environment list was reversed.
    candidates.sort(
        key=lambda candidate: os.path.normcase(
            os.path.abspath(os.fspath(candidate))
        )
    )
    sources: list[_RegularSourceFile] = []
    seen: set[tuple[int, int]] = set()
    unresolved_home_count = 0
    for candidate in candidates:
        path = candidate if candidate.name == "state.db" else candidate / "state.db"
        root = candidate.parent if candidate.name == "state.db" else candidate
        source = _regular_source_file(path, root=root)
        if source is None:
            unresolved_home_count += 1
            continue
        identity = (source.device, source.inode)
        if identity in seen:
            continue
        seen.add(identity)
        sources.append(source)
    return _HermesStateDbPlan(
        sources=tuple(sources),
        configured_home_count=len(candidates),
        unresolved_home_count=unresolved_home_count,
        explicit_home=hermes_home is not None,
    )


def _read_hermes_state_db_usage(
    db_source: _RegularSourceFile,
    *,
    limit_sessions: int,
) -> _HermesStateDbScan:
    con = _connect_regular_source_sqlite_read_only(db_source)
    con.row_factory = sqlite3.Row
    try:
        # cwd gives hermes rows a real project label (same data class already
        # imported for claude-code/codex). Column-conditional: older hermes
        # schemas without it keep importing. Deliberately NOT selected:
        # `title` and any other transcript-derived text (privacy line).
        columns = _sqlite_table_columns(con, "sessions")
        optional_columns = [column for column in ("cwd",) if column in columns]
        select_columns = [
            "id",
            "model",
            "billing_provider",
            "started_at",
            "message_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "estimated_cost_usd",
            "actual_cost_usd",
            *optional_columns,
        ]
        where_clause = "model is not null and trim(model) != ''"
        discovered_rows = int(
            con.execute(
                f"select count(*) from sessions where {where_clause}"
            ).fetchone()[0]
        )
        rows = con.execute(
            f"""
            select {", ".join(select_columns)}
            from sessions
            where {where_clause}
            order by started_at desc, id asc
            limit ?
            """,
            (max(0, limit_sessions),),
        ).fetchall()
    finally:
        con.close()
    usage_rows = []
    for row in rows:
        usage = dict(row)
        usage["cache_read_tokens_reported"] = "cache_read_tokens" in columns
        usage["cache_write_tokens_reported"] = "cache_write_tokens" in columns
        model = str(usage.get("model") or "").strip()
        if not model:
            continue
        actual_cost = _optional_float(usage.get("actual_cost_usd"))
        estimated_cost = _optional_float(usage.get("estimated_cost_usd"))
        if actual_cost is not None and actual_cost > 0:
            usage["cost_usd"] = actual_cost
            usage["cost_source"] = "hermes_actual_cost_usd"
        elif estimated_cost is not None and estimated_cost > 0:
            usage["cost_usd"] = estimated_cost
            usage["cost_source"] = "hermes_estimated_cost_usd"
        else:
            usage["cost_usd"] = None
            usage["cost_source"] = None
        usage["session_id"] = str(usage.get("id") or "")
        usage["model"] = model
        usage["provider"] = _normalize_hermes_provider(_limited_optional_text(usage.get("billing_provider"), 120), model)
        usage_rows.append(usage)
    return _HermesStateDbScan(
        discovered_rows=max(0, discovered_rows),
        selected_rows=tuple(usage_rows),
    )


def _event_has_usage_or_cost(event: ClientUsageEvent) -> bool:
    return any(
        [
            event.input_tokens > 0,
            event.output_tokens > 0,
            event.cached_input_tokens > 0,
            event.reasoning_output_tokens > 0,
            (event.client_reported_cost_usd or 0.0) > 0,
        ]
    )


def _normalize_hermes_provider(value: str | None, model: str) -> str:
    if value:
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"anthropic", "claude"}:
            return "anthropic"
        if normalized in {"openai", "openai_codex"}:
            return "openai"
        if normalized in {"google", "google_ai", "gemini", "vertex", "vertex_ai"}:
            return "google"
        if normalized:
            return normalized
    lowered = model.lower()
    if lowered.startswith("claude-") or lowered.startswith("claude/"):
        return "anthropic"
    if lowered.startswith("gpt") or lowered.startswith("chatgpt") or (lowered.startswith("o") and len(lowered) > 1 and lowered[1].isdigit()):
        return "openai"
    if lowered.startswith("gemini-") or lowered.startswith("gemini/"):
        return "google"
    return "hermes"


def _safe_nonnegative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


def _is_nonnegative_usage_token_value(value: Any) -> bool:
    """Validate a transcript token scalar without silently coercing schema drift."""

    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float):
        return value >= 0 and value.is_integer()
    if isinstance(value, str):
        stripped = value.strip()
        return bool(stripped) and stripped.isdigit()
    return False


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp_seconds(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        if not isinstance(value, str):
            return None
        from datetime import datetime

        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    if number <= 0:
        return None
    if number > 1_000_000_000_000:
        number = number / 1000
    return int(number)


def _limited_optional_text(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def _sanitized_session_title(value: Any) -> str | None:
    """Bound an explicit client session title without importing prompt text."""

    if not isinstance(value, str):
        return None
    printable: list[str] = []
    for char in value:
        if char.isspace():
            printable.append(" ")
        elif not unicodedata.category(char).startswith("C"):
            printable.append(char)
    title = " ".join("".join(printable).split())
    if not title:
        return None
    if len(title) > _MAX_SESSION_TITLE_LENGTH:
        return title[: _MAX_SESSION_TITLE_LENGTH - 1] + "…"
    return title


def _find_first_string_key(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        found = value.get(key)
        if isinstance(found, str):
            return found
        for child in value.values():
            result = _find_first_string_key(child, key)
            if result is not None:
                return result
    if isinstance(value, list):
        for child in value:
            result = _find_first_string_key(child, key)
            if result is not None:
                return result
    return None


def _env_text(name: str) -> str | None:
    import os

    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None
