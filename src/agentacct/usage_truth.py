from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .confidence import (
    COST_CLIENT_REPORTED,
    COST_ESTIMATED_FROM_TOKENS,
    COST_PROVIDER_BILLED,
    COST_UNKNOWN,
    USAGE_CLIENT_REPORTED,
    USAGE_PROVIDER_REPORTED,
    USAGE_UNKNOWN,
)

EvidenceTier = Literal[
    "event_workflow",
    "local_import",
    "provider_proxy",
    "process_wrapper",
    "mechanical_capture",
    "telemetry",
    "orchestrator_claim",
    "artifact",
]


@dataclass(frozen=True)
class UsageTruthRow:
    integration: str
    tier: EvidenceTier
    evidence_source: str
    update_timing: str
    observable_fields: list[str]
    usage_confidence: str
    cost_confidence: str
    hard_budget_basis: str
    automatic_scope: str
    setup_path: str
    limitations: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


USAGE_TRUTH_TABLE: tuple[UsageTruthRow, ...] = (
    UsageTruthRow(
        integration="codex local usage import",
        tier="local_import",
        evidence_source="~/.codex/state_5.sqlite plus ~/.codex/sessions/**/rollout-*.jsonl",
        update_timing=(
            "Codex emits repeated rollout token_count events with last_token_usage and total_token_usage during a thread; "
            "agentacct sees updates when import or a watcher reads the flushed JSONL."
        ),
        observable_fields=[
            "session id",
            "project cwd",
            "redacted title presence",
            "model when present",
            "non-cached input tokens",
            "cached input tokens",
            "output tokens",
            "reasoning output tokens",
            "turn count",
            "created/updated timestamps",
        ],
        usage_confidence=USAGE_CLIENT_REPORTED,
        cost_confidence=f"{COST_UNKNOWN}; optional {COST_ESTIMATED_FROM_TOKENS} with --estimate-costs when pricing exists",
        hard_budget_basis="No hard dollar enforcement from local import alone; use advisory warnings or token/runtime limits.",
        automatic_scope="Reads supported local Codex session stores only when the user runs import or grants a scan path.",
        setup_path="agentacct usage import-local --client codex --dry-run --json",
        limitations=[
            "Does not read provider invoices.",
            "Does not prove exact Codex subscription billing.",
            "Current local subscription samples expose token/cache/reasoning fields but no provider-billed cost field.",
            "Forked child/internal rollout cumulative counters are preserved but held from additive totals until parent replay is normalized.",
            "Does not automatically monitor unrelated Codex sessions without import or MCP setup.",
        ],
    ),
    UsageTruthRow(
        integration="claude-code local usage import",
        tier="local_import",
        evidence_source="~/.claude/projects/**/*.jsonl",
        update_timing=(
            "Claude Code stores token usage on assistant message rows as message.usage while transcript files are written; "
            "local journal/result rows were not the usage source in observed samples."
        ),
        observable_fields=[
            "session id",
            "project cwd",
            "redacted title presence",
            "model when present",
            "input tokens",
            "cache creation input tokens",
            "cache read input tokens",
            "output tokens",
            "turn count",
            "file updated timestamp",
        ],
        usage_confidence=USAGE_CLIENT_REPORTED,
        cost_confidence=f"{COST_UNKNOWN}; optional {COST_ESTIMATED_FROM_TOKENS} with --estimate-costs when pricing exists",
        hard_budget_basis="No hard dollar enforcement from local import alone; use advisory warnings or token/runtime limits.",
        automatic_scope="Reads supported local Claude Code project JSONL files only when the user runs import or grants a scan path.",
        setup_path="agentacct usage import-local --client claude-code --dry-run --json",
        limitations=[
            "Does not read Anthropic invoices.",
            "Does not prove exact Claude Code subscription billing.",
            "Current local subscription samples expose token/cache fields but no provider-billed cost field.",
            "Current importer does not store transcript content or raw prompts.",
        ],
    ),
    UsageTruthRow(
        integration="opencode JSON event stream import",
        tier="local_import",
        evidence_source="OpenCode JSON/JSONL event streams, including opencode run --format json output",
        update_timing="OpenCode reports usage and client cost on step-finish events in captured JSON streams.",
        observable_fields=[
            "session id",
            "model when present",
            "input tokens",
            "cached input tokens",
            "output tokens",
            "reasoning output tokens",
            "turn count",
            "client-reported cost when step-finish cost is present",
        ],
        usage_confidence=USAGE_CLIENT_REPORTED,
        cost_confidence=COST_CLIENT_REPORTED,
        hard_budget_basis="Not provider-billed; useful for dashboards and advisory budgets unless routed through a provider/proxy path.",
        automatic_scope="Reads exported/captured OpenCode JSON files from the supplied OpenCode home/export directory.",
        setup_path="agentacct usage import-local --client opencode --opencode-home /path --dry-run --json",
        limitations=[
            "Client-reported cost is only as reliable as OpenCode's emitted fields.",
            "Does not prove provider invoice totals.",
            "Requires JSON event output or captured local stream files.",
        ],
    ),
    UsageTruthRow(
        integration="hermes local state import",
        tier="local_import",
        evidence_source="~/.hermes/state.db sessions table",
        update_timing="Hermes records session-level model, token, and client cost fields in its local state database.",
        observable_fields=[
            "session id",
            "provider inferred from billing_provider or model",
            "model",
            "input tokens",
            "cache read/write tokens",
            "output tokens",
            "reasoning tokens",
            "message count",
            "started timestamp",
            "client-reported actual_cost_usd or estimated_cost_usd when present",
        ],
        usage_confidence=USAGE_CLIENT_REPORTED,
        cost_confidence=COST_CLIENT_REPORTED,
        hard_budget_basis="Not provider-billed; useful for dashboards and advisory budgets unless routed through a provider/proxy path.",
        automatic_scope="Reads Hermes local state.db only when the user runs import or grants a scan path.",
        setup_path="agentacct usage import-local --client hermes --dry-run --json",
        limitations=[
            "Client-reported Hermes cost fields are not provider invoices.",
            "Does not store prompts or transcript content.",
            "Only covers sessions present in the local Hermes state database.",
        ],
    ),
    UsageTruthRow(
        integration="openclaw JSONL local usage import",
        tier="local_import",
        evidence_source="OpenClaw JSONL session logs under configured OpenClaw roots",
        update_timing="OpenClaw assistant message rows can carry usage token fields and optional usage.cost.total values.",
        observable_fields=[
            "session id",
            "provider/model from model_change or message rows",
            "input tokens",
            "cache read/write tokens",
            "output tokens",
            "turn count",
            "client-reported cost when usage.cost.total is present",
        ],
        usage_confidence=USAGE_CLIENT_REPORTED,
        cost_confidence=f"{COST_CLIENT_REPORTED} when usage.cost.total is present; otherwise {COST_UNKNOWN}",
        hard_budget_basis="Not provider-billed; useful for dashboards and advisory budgets unless routed through a provider/proxy path.",
        automatic_scope="Reads supported local OpenClaw JSONL logs only when the user runs import or grants a scan path.",
        setup_path="agentacct usage import-local --client openclaw --dry-run --json",
        limitations=[
            "Client-reported OpenClaw cost fields are not provider invoices.",
            "Does not store prompts or transcript content.",
            "Only covers logs present in the configured OpenClaw roots.",
        ],
    ),
    UsageTruthRow(
        integration="agent MCP workflow events",
        tier="event_workflow",
        evidence_source="agentacct MCP tools called by Claude Code, Codex, Hermes, OpenCode, OpenClaw, or another MCP-capable agent",
        update_timing="Updates arrive only when the configured agent calls agentacct MCP tools.",
        observable_fields=[
            "section started/checkpoint/completed/blocked events",
            "source agent label",
            "run id when supplied",
            "machine-check evidence when supplied",
            "optional token/cost metadata if the agent reports it",
        ],
        usage_confidence=USAGE_UNKNOWN,
        cost_confidence=COST_UNKNOWN,
        hard_budget_basis="MCP events alone do not provide hard dollar enforcement; combine with import/proxy/token/runtime policy.",
        automatic_scope="Only records what the configured agent explicitly reports through agentacct MCP tools.",
        setup_path="agentacct init --agent codex; agentacct mcp doctor",
        limitations=[
            "MCP setup does not automatically parse private session logs.",
            "MCP setup does not imply exact billing.",
            "Agent instructions must be followed by the client to produce events.",
        ],
    ),
    UsageTruthRow(
        integration="native coding-agent hook capture",
        tier="mechanical_capture",
        evidence_source="Claude Code, Codex, or Cursor host hook JSON normalized through the metadata-only capture adapter",
        update_timing="One bounded local write when the configured host invokes a rendered opt-in hook.",
        observable_fields=[
            "session/turn/tool identifiers when exposed",
            "lifecycle event type",
            "relative file paths or counts when exposed",
            "recognized test/build/lint/typecheck exit code",
            "subagent relationship when exposed",
        ],
        usage_confidence=USAGE_UNKNOWN,
        cost_confidence=COST_UNKNOWN,
        hard_budget_basis="No token or dollar enforcement; hooks are mechanical activity evidence, not billing telemetry.",
        automatic_scope="Only the host events explicitly enabled by an opt-in local hook manifest; no global config is written automatically.",
        setup_path="agentacct capture manifest --vendor codex",
        limitations=[
            "Does not store prompts, responses, thoughts, tool arguments, tool results, stdout, or stderr.",
            "Does not capture usage or cost.",
            "Host coverage and stable identifiers vary by client and event.",
        ],
    ),
    UsageTruthRow(
        integration="OpenLIT / OTLP trace ingestion",
        tier="telemetry",
        evidence_source="OTLP/HTTP JSON spans exported by OpenLIT or another local OpenTelemetry-compatible producer",
        update_timing="As spans are exported to the local /v1/traces receiver, or when an OTLP JSON file is imported.",
        observable_fields=[
            "trace/span/session identifiers",
            "runtime lifecycle and tool-span metadata",
            "allowlisted model/provider labels",
            "telemetry-reported token or cost fields when present",
        ],
        usage_confidence="telemetry_reported; corroborating until its measurement provenance is independently verified",
        cost_confidence="telemetry_reported; never provider_billed merely because it arrived over OTLP",
        hard_budget_basis="Advisory only unless a separate provider-billed source proves the cost basis.",
        automatic_scope="Only spans intentionally exported or imported into agentacct's local receiver.",
        setup_path="agentacct connector openlit /path/to/otlp.json --dry-run --json",
        limitations=[
            "OTLP is a transport format, not proof of billing authority.",
            "agentacct discards prompt, response, thought, tool argument, and tool result attributes.",
            "Coverage depends on what the instrumented runtime emits.",
        ],
    ),
    UsageTruthRow(
        integration="Paperclip snapshot connector",
        tier="orchestrator_claim",
        evidence_source="User-supplied Paperclip exported JSON snapshot",
        update_timing="Only when a snapshot is explicitly imported.",
        observable_fields=[
            "company/agent/work-item identifiers",
            "execution assignment and status claims",
            "work-product claims",
            "orchestrator-reported cost claim or explicit missing value",
        ],
        usage_confidence=USAGE_UNKNOWN,
        cost_confidence="orchestrator_claim; zero and missing remain distinct",
        hard_budget_basis="Advisory only; agentacct never dispatches an external Paperclip action.",
        automatic_scope="Read-only parsing of the supplied export; no Paperclip API mutation or credential is used.",
        setup_path="agentacct connector paperclip /path/to/snapshot.json --dry-run --json",
        limitations=[
            "Orchestrator claims are not provider invoices or objective machine results.",
            "Connector does not copy Paperclip's scheduler, approvals, RBAC, or control plane.",
        ],
    ),
    UsageTruthRow(
        integration="Entire Git checkpoint connector",
        tier="artifact",
        evidence_source="Known public Entire checkpoint refs, commit trailers, and allowlisted metadata.json scalars",
        update_timing="Only when a local repository is explicitly scanned.",
        observable_fields=[
            "commit/checkpoint/session identifiers",
            "public ref and parent count",
            "allowlisted change-count metadata",
            "checkpoint-reported usage fields when present",
        ],
        usage_confidence="checkpoint_reported; corroborating only",
        cost_confidence=COST_UNKNOWN,
        hard_budget_basis="No hard budget basis; Git proves artifact existence, not billing or task completion.",
        automatic_scope="Read-only Git plumbing against known Entire refs; refs, index, and worktree are never changed.",
        setup_path="agentacct connector entire /path/to/repo --dry-run --json",
        limitations=[
            "Does not ingest prompts, transcripts, messages, or private checkpoint bodies.",
            "Line-to-prompt attribution remains heuristic.",
        ],
    ),
    UsageTruthRow(
        integration="provider/API proxy",
        tier="provider_proxy",
        evidence_source="Traffic explicitly routed through agentacct provider forwarding",
        update_timing=(
            "Request estimates are available before forwarding; provider-reported usage/cost is available after each proxied response."
        ),
        observable_fields=[
            "provider",
            "model",
            "request token estimate",
            "provider-reported token usage when returned",
            "provider-billed cost when returned as finite non-negative provider cost",
            "budget decision",
        ],
        usage_confidence=USAGE_PROVIDER_REPORTED,
        cost_confidence=f"{COST_PROVIDER_BILLED} when provider returns billed cost; otherwise {COST_ESTIMATED_FROM_TOKENS}",
        hard_budget_basis="Strongest hard dollar enforcement path when traffic flows through agentacct and budget caps are configured.",
        automatic_scope="Only covers API traffic intentionally sent through agentacct's proxy/forwarding endpoint.",
        setup_path="agentacct cost proxy --enable-forwarding --forward-provider openai --max-total-usd 0.01",
        limitations=[
            "Does not cover subscription clients that bypass the proxy.",
            "Provider usage and cost fields vary by provider.",
            "Requires explicit provider allowlist and environment-provided API keys.",
        ],
    ),
    UsageTruthRow(
        integration="sentinel-owned process wrapper",
        tier="process_wrapper",
        evidence_source="Commands launched through agentacct run or sentinel-* wrappers",
        update_timing="Process state can update while agentacct owns the process; token/cost freshness requires import, proxy, or MCP events.",
        observable_fields=[
            "process status",
            "runtime",
            "stdout/stderr logs",
            "exit code",
            "repeated stderr patterns",
            "pause/resume/kill ownership metadata",
            "run report artifacts",
        ],
        usage_confidence=USAGE_UNKNOWN,
        cost_confidence=COST_UNKNOWN,
        hard_budget_basis="Useful for runtime/process control; not a token or billing source unless paired with importer/proxy/events.",
        automatic_scope="Only commands the user explicitly launches through agentacct.",
        setup_path="agentacct run -- <command>",
        limitations=[
            "Not the default onboarding path for normal agent workflows.",
            "Cannot see provider internals by itself.",
            "Best treated as an optional fallback for tasks that need process ownership.",
        ],
    ),
)


def usage_truth_table() -> list[dict[str, object]]:
    return [row.to_dict() for row in USAGE_TRUTH_TABLE]


# FROZEN STORED-DATA VOCABULARY: historical stores on real machines contain
# these strings forever; the product renamed to agentacct but stored
# identifiers are format, not branding. Never rename; extend matchers
# additively only. (Same rule pins the schema-version strings, event sources,
# metadata keys, and proxy wire vocabulary at their definition sites.)
LOCAL_USAGE_SOURCE = "local_client_session_store"
LOCAL_USAGE_PROVENANCE = "agent_sentinel_local_usage_import"

# A local client can prove that a session exists before (or without) emitting
# a usable token row.  Keep that fact in a separate, server-trusted lane: it
# must never be interpreted as measured zero usage or as hook/mechanical
# capture.  These strings are frozen stored-data vocabulary once shipped.
LOCAL_SESSION_OBSERVATION_SOURCE = "local_client_session_store_observation"
LOCAL_SESSION_OBSERVATION_PROVENANCE = "agent_chronicle_local_session_observation_import"

_SESSION_OBSERVATION_TOP_LEVEL_FIELDS = frozenset(
    {"event_id", "created_at", "source", "event_type", "run_id", "metadata"}
)
_SESSION_OBSERVATION_METADATA_FIELDS = frozenset(
    {
        "observation_source",
        "observation_provenance",
        "session_observation_schema_version",
        "observation_revision",
        "client",
        "client_session_id",
        "client_session_kind",
        "parent_client_session_id",
        "client_transcript_id",
        "client_spawn_status",
        "client_thread_source",
        "source_file",
        "source_namespace_fingerprint",
        "parent_source_namespace_fingerprint",
        "project_dir",
        "client_session_title",
        "client_session_title_source",
        "client_session_title_sanitized",
        "title_redacted",
        "observed_models",
        "started_at",
        "updated_at",
        "source_updated_at",
        "source_updated_at_basis",
        "source_revision_at",
        "source_revision_basis",
        "observation_basis",
        "activity_time_basis",
        "source_parse_complete",
        "evidenced_event_ids",
        "evidenced_event_id_total",
        "evidenced_outputs_skipped",
        # Server-authored value-redaction provenance (service.py's
        # RESERVED_VALUE_REDACTION_KEYS; spelled literally here because
        # service imports this module, not the other way round, and a test
        # pins the two lists together). An observation title or project path
        # can carry a credential, and the redaction is applied before this
        # rebuild -- without these the cut would happen silently, which is
        # the exact failure the marker exists to prevent. A caller cannot
        # forge them: redact_event_secrets strips caller copies first.
        "value_redaction_applied",
        "value_redaction_fields",
    }
)

# Usage/cost vocabulary is forbidden on a session-observation event.  The
# trusted writer removes it defensively, so downstream readers can rely on the
# lane being existence/activity evidence only.  Metadata fields used by local
# usage rows are removed for the same reason; identity/evidence-link fields are
# intentionally retained.
_SESSION_OBSERVATION_TOP_LEVEL_USAGE_FIELDS = frozenset(
    {
        "provider",
        "model",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "estimated_cost_usd",
        "actual_cost_usd",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "usage_confidence",
        "cost_confidence",
    }
)
_SESSION_OBSERVATION_METADATA_USAGE_FIELDS = frozenset(
    {
        "usage_source",
        "usage_provenance",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "reasoning_output_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "actual_cost_usd",
        "estimated_cost_usd",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "usage_additive",
        "usage_update_semantics",
        "usage_row_lane",
    }
)

# "Instrumentation installed" markers: CLI-authored per-client events that let
# the ledger classify sessions as pre/post instrumentation (a session that ran
# before install can never have MCP work context, and views must say so
# instead of rendering it as product failure). Same trust model as usage
# provenance: the provenance value below is stamped only by the CLI marker
# writers via SentinelService.record_event(trusted_instrumentation_marker=True);
# every other recording path strips it, so forged agent/HTTP events never
# classify anything.
INSTRUMENTATION_MARKER_EVENT_TYPE = "instrumentation_installed"
# frozen: renaming this provenance value would orphan every existing marker
# and reclassify history as pre-instrumentation.
CLI_INSTRUMENTATION_PROVENANCE = "agent_sentinel_cli_instrumentation_marker"

# agentacct's own diagnostic tooling writes probe events with these exact
# type/source signatures (cli `mcp doctor --write-probe` and `mcp
# workflow-smoke`). They are self-test traffic, never user work, and must not
# reach user-facing ledger/bridge builders. If a new diagnostic writer is
# added, its event_type/source MUST be registered here or it will leak into
# user views (the writers carry a pointer comment back to these constants).
DIAGNOSTIC_EVENT_TYPES = frozenset({"mcp_doctor_test", "workflow_smoke"})
# frozen source strings (pre-rename): stored in events forever; the writers
# keep emitting them so old and new diagnostic events classify identically.
DIAGNOSTIC_EVENT_SOURCES = frozenset({"agent-sentinel-mcp-doctor", "agent-sentinel-mcp-workflow-smoke"})
# Default run_ids of the diagnostic writers — exported for raw-tab LABELING
# only. run_id is user-overridable free text, so it is NEVER a filter key: a
# user collision must not hide real work (missing beats wrong applies to
# display in both directions).
DIAGNOSTIC_RUN_IDS = frozenset({"mcp_doctor_test", "mcp_workflow_smoke"})


def is_diagnostic_event(event: dict[str, Any]) -> bool:
    """True for agentacct's own diagnostic tool events (doctor probe, workflow smoke).

    Matches event_type OR source — deliberately never run_id-only (see
    DIAGNOSTIC_RUN_IDS). Distinct from the work ledger's
    ``build_diagnostic_usage_events`` concept (model_usage rows that are not
    trusted import rows): this predicate identifies agentacct SELF-TEST events,
    and the excluded bucket is named ``diagnostic_tool_events`` everywhere to
    avoid colliding with that existing vocabulary.
    """

    if str(event.get("event_type") or "") in DIAGNOSTIC_EVENT_TYPES:
        return True
    return str(event.get("source") or "") in DIAGNOSTIC_EVENT_SOURCES


def split_diagnostic_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split events into (user_facing, diagnostic_tool_events), order preserved.

    User-facing ledger/bridge builders consume the first list; the second is
    surfaced as a labeled count (never silently dropped). Raw event surfaces
    (`service.list_events`, `summarize_events`, /events) are NOT filtered —
    the raw log keeps diagnostics visible, labeled.
    """

    user_facing: list[dict[str, Any]] = []
    diagnostic: list[dict[str, Any]] = []
    for event in events:
        if is_diagnostic_event(event):
            diagnostic.append(event)
        else:
            user_facing.append(event)
    return user_facing, diagnostic

# Legacy Claude Code key artifact: mid-session model switches used to mutate the
# client_session_id to "<session>:model:<sanitized-model>", splitting one client
# session into several rows and double-counting usage. New rows never contain
# this marker; it survives only in stores written before the fix.
LOCAL_USAGE_MODEL_KEY_MARKER = ":model:"

# Per-model "lane" tag carried in metadata.usage_row_lane on claude-code rows.
# Row identity is (client, base session id, lane) so per-model rows of one
# session share a stable session key without ever merging across models.
USAGE_ROW_LANE_PREFIX = "model:"

USAGE_KEY_MIGRATION_REASON = "claude_code_session_key_unification"

# Codex rollout ``total_token_usage`` counters are cumulative within a
# thread.  A fork starts by replaying much of its parent's rollout history,
# so an un-normalized child/internal row is NOT an additive session total.
# Keep the legacy row for identity/forensics, but never add it to product
# usage or cost until the importer has proven and stored an exclusive lineage
# delta under this versioned semantic.
CODEX_LINEAGE_DELTA_SEMANTICS = "codex_rollout_lineage_delta_v1"
CODEX_REPLAY_QUARANTINE_STATE = "legacy_codex_descendant_cumulative_unproven"


def local_usage_additivity(
    *,
    client: object,
    session_kind: object = None,
    parent_client_session_id: object = None,
    usage_update_semantics: object = None,
    explicit_usage_additive: object = None,
    source_namespace_fingerprint: object = None,
    parent_source_namespace_fingerprint: object = None,
) -> tuple[bool, str | None]:
    """Return whether a trusted local usage row is safe to add.

    Only Codex descendant rows need the replay guard.  Root Codex rows and
    other importers retain their existing additive semantics.  This read-time
    classifier intentionally has no row-local escape hatch: a normalized
    descendant can become additive only after a cohort-level validator proves
    that its parent exists in the same source namespace and the stored delta
    is atomic.  An explicit false bit is honored for every client so an
    importer can quarantine a row without relying on client-specific inference.
    """

    if explicit_usage_additive is False:
        return False, CODEX_REPLAY_QUARANTINE_STATE if str(client or "") == "codex" else "importer_reported_non_additive"

    normalized_client = str(client or "").strip().lower()
    if normalized_client != "codex":
        return True, None

    kind = str(session_kind or "root").strip().lower()
    is_descendant = kind in {"child", "internal"} or bool(str(parent_client_session_id or "").strip())
    if not is_descendant:
        return True, None

    # A descendant becomes additive only after the importer has replaced its
    # inherited rollout counters with a source-local, event-level delta.  The
    # namespace equality is part of the proof: a bare parent id from another
    # configured Codex home must never release a quarantined row.
    parent_session = (
        parent_client_session_id.strip()
        if isinstance(parent_client_session_id, str)
        else ""
    )
    source_namespace = (
        source_namespace_fingerprint.strip()
        if isinstance(source_namespace_fingerprint, str)
        else ""
    )
    parent_namespace = (
        parent_source_namespace_fingerprint.strip()
        if isinstance(parent_source_namespace_fingerprint, str)
        else ""
    )
    if (
        str(usage_update_semantics or "").strip() == CODEX_LINEAGE_DELTA_SEMANTICS
        and parent_session
        and source_namespace
        and source_namespace == parent_namespace
    ):
        return True, None

    return False, CODEX_REPLAY_QUARANTINE_STATE


def local_usage_event_additivity(event: dict[str, Any]) -> tuple[bool, str | None]:
    """Classify one trusted local-import event through the shared guard."""

    metadata = event.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    client = metadata.get("client")
    if not client:
        source = str(event.get("source") or "")
        suffix = "-local-session-import"
        client = source[: -len(suffix)] if source.endswith(suffix) else source
    return local_usage_additivity(
        client=client,
        session_kind=metadata.get("client_session_kind"),
        parent_client_session_id=metadata.get("parent_client_session_id"),
        usage_update_semantics=metadata.get("usage_update_semantics"),
        explicit_usage_additive=metadata.get("usage_additive"),
        source_namespace_fingerprint=metadata.get("source_namespace_fingerprint"),
        parent_source_namespace_fingerprint=metadata.get("parent_source_namespace_fingerprint"),
    )


def sanitize_session_key_component(value: object) -> str:
    """Sanitize a session-key/lane component to [alnum, '_', '-'], max 80 chars.

    This is the exact historical logic of the legacy ':model:' suffix writer, so
    lanes derived from raw model strings equal the suffixes embedded in legacy
    keys for the same model. Distinct raw strings can collide after
    sanitization (e.g. 'a.b' vs 'a_b'); the consequence is co-refresh of the
    pair inside one replacement set, never double-counting.
    """

    text = str(value or "unknown")
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text)
    return safe[:80] or "unknown"


def local_usage_base_session_id(session_id: str) -> str:
    """Strip our legacy ':model:' key artifact; child ':stem' suffixes are preserved."""

    base, _, _ = session_id.partition(LOCAL_USAGE_MODEL_KEY_MARKER)
    return base


def normalized_local_usage_session_id(client: object, session_id: str) -> str:
    """Client-gated base normalization: only the Claude Code importer ever wrote
    the ':model:' marker, so only claude-code keys are stripped. A non-claude
    session id that happens to contain ':model:' is user data — stripping it
    could merge unrelated sessions (missing beats wrong)."""

    if str(client or "") != "claude-code":
        return session_id
    return local_usage_base_session_id(session_id)


def legacy_suffixed_claude_code_bases(events: list[dict[str, Any]]) -> set[str]:
    """Base session ids that still have legacy ':model:'-suffixed claude-code rows.

    Only trusted local usage import rows for the claude-code client are
    considered: the ':model:' marker is our own historical key artifact and is
    never interpreted on other clients' ids (missing beats wrong).
    """

    bases: set[str] = set()
    for event in events:
        if not is_local_usage_import_event(event):
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, dict) or str(metadata.get("client") or "") != "claude-code":
            continue
        session_id = str(metadata.get("client_session_id") or "")
        if LOCAL_USAGE_MODEL_KEY_MARKER in session_id:
            bases.add(local_usage_base_session_id(session_id))
    return bases


def is_shadowed_legacy_usage_import_event(event: dict[str, Any], suffixed_bases: set[str]) -> bool:
    """True for a stale claude-code base row shadowed by ':model:' sibling rows.

    The pre-fix Claude Code importer re-keyed a session's rows to
    '<base>:model:<m>' on a mid-session model switch while the earlier
    base-keyed row stayed frozen with partial totals — the historical
    double-count pairing that the one-shot key migration supersedes. Until an
    import runs the migration, un-migrated stores still contain BOTH shapes;
    reading the stale base row would double-count the session's usage and
    (after read-time base normalization) double-attribute it to sections at
    high confidence. Every read surface (work ledger, dashboard, context
    bridge) therefore excludes it via this one shared rule so attributed
    totals equal true usage and all surfaces agree byte-for-byte.

    New-scheme lane rows always carry metadata.usage_row_lane and are never
    shadowed; non-claude-code rows are never touched.
    """

    if not suffixed_bases or not is_local_usage_import_event(event):
        return False
    metadata = event.get("metadata")
    if not isinstance(metadata, dict) or str(metadata.get("client") or "") != "claude-code":
        return False
    if metadata.get("usage_row_lane"):
        return False
    session_id = str(metadata.get("client_session_id") or "")
    if LOCAL_USAGE_MODEL_KEY_MARKER in session_id:
        return False
    return session_id in suffixed_bases


def split_shadowed_legacy_usage_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split events into (kept, shadowed legacy base usage rows).

    Non-usage events and all rows that are not stale shadowed claude-code base
    rows pass through unchanged, in order.
    """

    suffixed_bases = legacy_suffixed_claude_code_bases(events)
    if not suffixed_bases:
        return list(events), []
    kept: list[dict[str, Any]] = []
    shadowed: list[dict[str, Any]] = []
    for event in events:
        if is_shadowed_legacy_usage_import_event(event, suffixed_bases):
            shadowed.append(event)
        else:
            kept.append(event)
    return kept, shadowed


def local_usage_row_lane(event: dict[str, Any]) -> str | None:
    """Per-model lane of a stored local usage import row, or None for non-claude rows.

    Codex/hermes/opencode/openclaw model labels can drift between imports, so
    model never enters their row identity (lane None -> '').
    """

    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return None
    lane = metadata.get("usage_row_lane")
    if isinstance(lane, str) and lane:
        return lane
    if str(metadata.get("client") or "") != "claude-code":
        return None
    session_id = str(metadata.get("client_session_id") or "")
    if LOCAL_USAGE_MODEL_KEY_MARKER in session_id:
        return USAGE_ROW_LANE_PREFIX + session_id.partition(LOCAL_USAGE_MODEL_KEY_MARKER)[2]
    return USAGE_ROW_LANE_PREFIX + sanitize_session_key_component(event.get("model") or "unknown")


def local_usage_row_identity(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """Stable (client, base session id, lane) identity of a stored import row."""

    key = local_usage_event_key(event)
    if key is None:
        return None
    client, session_id = key
    lane = local_usage_row_lane(event) or ""
    return (client, normalized_local_usage_session_id(client, session_id), lane)


def local_usage_source_namespace_ambiguous_identities(
    events: list[dict[str, Any]],
) -> set[tuple[str, str, str]]:
    """Logical usage identities with explicit-vs-missing source provenance.

    Distinct explicit homes are distinct real facts and remain additive. A
    legacy missing-home row paired with any explicit home cannot be classified
    as duplicate or independent, so every row in that cohort must be held out
    of token/cost totals until reconciliation.
    """

    sources_by_identity: dict[
        tuple[str, str, str], set[str | None]
    ] = {}
    for event in events:
        if not is_local_usage_import_event(event):
            continue
        identity = local_usage_row_identity(event)
        if identity is None:
            continue
        metadata = event.get("metadata")
        raw_source = (
            metadata.get("source_namespace_fingerprint")
            if isinstance(metadata, dict)
            else None
        )
        source = (
            raw_source.strip()
            if isinstance(raw_source, str) and raw_source.strip()
            else None
        )
        sources_by_identity.setdefault(identity, set()).add(source)
    return {
        identity
        for identity, sources in sources_by_identity.items()
        if None in sources and any(source is not None for source in sources)
    }


def local_usage_event_key(event: dict[str, Any]) -> tuple[str, str] | None:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return None
    client = metadata.get("client")
    session_id = metadata.get("client_session_id")
    if client and session_id:
        return (str(client), str(session_id))
    return None


def is_local_usage_import_event(event: dict[str, Any]) -> bool:
    metadata = event.get("metadata")
    return (
        event.get("event_type") == "model_usage"
        and isinstance(metadata, dict)
        and metadata.get("usage_source") == LOCAL_USAGE_SOURCE
        and metadata.get("usage_provenance") == LOCAL_USAGE_PROVENANCE
        and local_usage_event_key(event) is not None
    )


def is_legacy_local_usage_import_shape(event: dict[str, Any]) -> bool:
    metadata = event.get("metadata")
    source = str(event.get("source") or "")
    return (
        event.get("event_type") == "model_usage"
        and isinstance(metadata, dict)
        and metadata.get("usage_source") == LOCAL_USAGE_SOURCE
        and source.endswith("-local-session-import")
        and local_usage_event_key(event) is not None
    )


def recognized_local_usage_row_identity(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return a row identity only for trusted or supported legacy usage rows.

    ``local_usage_row_identity`` intentionally parses identity-shaped metadata
    without deciding whether the event is usage truth.  That is useful while
    constructing candidates, but it is unsafe as a ledger-wide dedup key: an
    ordinary MCP/work event can legitimately carry ``metadata.client`` and
    ``client_session_id`` and must never suppress a real ``model_usage`` row.
    Keep the trust/legacy-shape gate in one shared wrapper for write-time dedup
    and race classification.
    """

    if not (is_local_usage_import_event(event) or is_legacy_local_usage_import_shape(event)):
        return None
    return local_usage_row_identity(event)


def mark_trusted_local_usage_import_event(event: dict[str, Any]) -> dict[str, Any]:
    recorded = dict(event)
    metadata = recorded.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata["usage_source"] = LOCAL_USAGE_SOURCE
    metadata["usage_provenance"] = LOCAL_USAGE_PROVENANCE
    metadata.pop("reserved_usage_source_stripped", None)
    metadata.pop("reserved_usage_provenance_stripped", None)
    recorded["metadata"] = metadata
    return recorded


def strip_untrusted_usage_truth_metadata(event: dict[str, Any]) -> dict[str, Any]:
    recorded = dict(event)
    metadata = recorded.get("metadata")
    if not isinstance(metadata, dict):
        return recorded
    sanitized = dict(metadata)
    if sanitized.get("usage_source") == LOCAL_USAGE_SOURCE:
        sanitized.pop("usage_source", None)
        sanitized["reserved_usage_source_stripped"] = True
    if "usage_provenance" in sanitized:
        sanitized.pop("usage_provenance", None)
        sanitized["reserved_usage_provenance_stripped"] = True
    recorded["metadata"] = sanitized
    return recorded


def local_session_observation_event_key(event: dict[str, Any]) -> tuple[str, str] | None:
    """Stable raw identity of one local client-session observation."""

    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return None
    client = metadata.get("client")
    session_id = metadata.get("client_session_id")
    if not (
        isinstance(client, str)
        and client.strip()
        and len(client) <= 80
        and isinstance(session_id, str)
        and session_id.strip()
        and len(session_id) <= 512
    ):
        return None
    return (client.strip(), session_id.strip())


def local_session_observation_source_watermark(
    event: dict[str, Any],
) -> int | float | None:
    """Client-source revision time; never fall back to agentacct arrival time.

    ``source_revision_at`` is the high-resolution canonical ordering field.
    ``source_updated_at`` and ``updated_at`` remain additive fallbacks for
    trusted rows created by the first importer build.
    """

    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return None
    for key in ("source_revision_at", "source_updated_at", "updated_at"):
        value = metadata.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    return None


def local_session_observation_revision(event: dict[str, Any]) -> str:
    """Server-derived content digest for revision/idempotency decisions.

    Caller-provided idempotency and revision strings are excluded.  Server
    arrival identity/time are excluded too, so identical re-imports compare
    equal while genuinely changed source facts do not.
    """

    metadata = event.get("metadata")
    canonical_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    for key in (
        "idempotency_key",
        "observation_revision",
        "observation_provenance",
        "reserved_observation_source_stripped",
        "reserved_observation_provenance_stripped",
    ):
        canonical_metadata.pop(key, None)
    material = {
        "source": event.get("source"),
        "event_type": event.get("event_type"),
        "run_id": event.get("run_id"),
        "metadata": canonical_metadata,
    }
    rendered = json.dumps(
        material,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def is_local_session_observation_event(event: dict[str, Any]) -> bool:
    """True only for a server-trusted, usage-free local observation row."""

    metadata = event.get("metadata")
    if not (
        event.get("event_type") == "session_observed"
        and isinstance(metadata, dict)
        and metadata.get("observation_source") == LOCAL_SESSION_OBSERVATION_SOURCE
        and metadata.get("observation_provenance") == LOCAL_SESSION_OBSERVATION_PROVENANCE
        and metadata.get("source_parse_complete") is True
        and isinstance(metadata.get("source_namespace_fingerprint"), str)
        and bool(metadata.get("source_namespace_fingerprint"))
        and local_session_observation_event_key(event) is not None
    ):
        return False
    if not set(event).issubset(_SESSION_OBSERVATION_TOP_LEVEL_FIELDS):
        return False
    if not set(metadata).issubset(_SESSION_OBSERVATION_METADATA_FIELDS):
        return False
    if any(key in event for key in _SESSION_OBSERVATION_TOP_LEVEL_USAGE_FIELDS):
        return False
    return not any(key in metadata for key in _SESSION_OBSERVATION_METADATA_USAGE_FIELDS)


def mark_trusted_local_session_observation_event(event: dict[str, Any]) -> dict[str, Any]:
    """Stamp server-only provenance and remove every token/cost field."""

    recorded = {
        key: value
        for key, value in event.items()
        if key in _SESSION_OBSERVATION_TOP_LEVEL_FIELDS and key != "metadata"
    }
    raw_metadata = event.get("metadata")
    metadata = {
        key: value
        for key, value in (
            raw_metadata.items() if isinstance(raw_metadata, dict) else []
        )
        if key in _SESSION_OBSERVATION_METADATA_FIELDS
    }
    metadata["observation_source"] = LOCAL_SESSION_OBSERVATION_SOURCE
    metadata["observation_provenance"] = LOCAL_SESSION_OBSERVATION_PROVENANCE
    metadata.pop("reserved_observation_source_stripped", None)
    metadata.pop("reserved_observation_provenance_stripped", None)
    if local_session_observation_source_watermark(
        {"metadata": {"source_updated_at": metadata.get("source_updated_at")}}
    ) is None:
        activity_updated_at = local_session_observation_source_watermark(
            {"metadata": {"updated_at": metadata.get("updated_at")}}
        )
        if activity_updated_at is not None:
            metadata["source_updated_at"] = activity_updated_at
    watermark = local_session_observation_source_watermark({"metadata": metadata})
    if watermark is not None:
        metadata["source_revision_at"] = watermark
    metadata["observation_revision"] = local_session_observation_revision(
        {**recorded, "metadata": metadata}
    )
    # Generic event idempotency is intentionally not used for this lane.  A
    # dedicated locked writer compares trusted canonical digests only, so an
    # untrusted row cannot pre-occupy the key.
    metadata.pop("idempotency_key", None)
    recorded["metadata"] = metadata
    return recorded


def strip_untrusted_session_observation_metadata(event: dict[str, Any]) -> dict[str, Any]:
    """Remove server-owned observation authority from generic writes."""

    recorded = dict(event)
    metadata = recorded.get("metadata")
    if not isinstance(metadata, dict):
        return recorded
    sanitized = dict(metadata)
    if sanitized.get("observation_source") == LOCAL_SESSION_OBSERVATION_SOURCE:
        sanitized.pop("observation_source", None)
        sanitized["reserved_observation_source_stripped"] = True
    if "observation_provenance" in sanitized:
        sanitized.pop("observation_provenance", None)
        sanitized["reserved_observation_provenance_stripped"] = True
    # A caller-provided digest has no authority either.  Keeping it would make
    # raw views imply that the server validated this revision.
    sanitized.pop("observation_revision", None)
    recorded["metadata"] = sanitized
    return recorded


def reduce_local_session_observation_events(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Select the latest authoritative row per raw client/session identity.

    Ordering uses the client source watermark, never agentacct ``created_at``.
    Different source namespaces, an unorderable revision cohort, or different
    content at the same watermark quarantine the complete identity group.
    Raw rows remain in storage for audit.
    """

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    diagnostics = {
        "trusted_rows": 0,
        "selected_sessions": 0,
        "historical_revisions_collapsed": 0,
        "namespace_conflict_sessions": 0,
        "watermark_conflict_sessions": 0,
        "unorderable_revision_sessions": 0,
        "quarantined_rows": 0,
    }
    for event in events:
        if not is_local_session_observation_event(event):
            continue
        key = local_session_observation_event_key(event)
        if key is None:
            continue
        diagnostics["trusted_rows"] += 1
        groups.setdefault(key, []).append(event)

    selected: list[dict[str, Any]] = []
    for rows in groups.values():
        namespace_values = {
            (
                metadata.get("source_namespace_fingerprint")
                if isinstance((metadata := row.get("metadata")), dict)
                and isinstance(metadata.get("source_namespace_fingerprint"), str)
                and metadata.get("source_namespace_fingerprint")
                else None
            )
            for row in rows
        }
        if len(namespace_values) != 1:
            diagnostics["namespace_conflict_sessions"] += 1
            diagnostics["quarantined_rows"] += len(rows)
            continue

        all_revisions = {local_session_observation_revision(row) for row in rows}
        if len(all_revisions) == 1:
            selected.append(rows[0])
            diagnostics["historical_revisions_collapsed"] += max(0, len(rows) - 1)
            continue

        watermarked = [(local_session_observation_source_watermark(row), row) for row in rows]
        missing_watermark = [row for watermark, row in watermarked if watermark is None]
        if missing_watermark and len(rows) > 1:
            diagnostics["unorderable_revision_sessions"] += 1
            diagnostics["quarantined_rows"] += len(rows)
            continue
        if missing_watermark:
            latest_rows = missing_watermark
        else:
            # File mtime revisions are nanosecond integers (~1e18). Casting to
            # float collapses nearby real revisions above 2**53 and can leave
            # no row equal to the rounded maximum, falsely quarantining a
            # perfectly ordered cohort. Keep the parser's exact numeric value.
            latest_watermark = max(
                watermark
                for watermark, _row in watermarked
                if watermark is not None
            )
            latest_rows = [row for watermark, row in watermarked if watermark == latest_watermark]
        revisions = {local_session_observation_revision(row) for row in latest_rows}
        if len(revisions) != 1:
            diagnostics["watermark_conflict_sessions"] += 1
            diagnostics["quarantined_rows"] += len(rows)
            continue
        selected.append(latest_rows[0])
        diagnostics["historical_revisions_collapsed"] += max(0, len(rows) - 1)

    diagnostics["selected_sessions"] = len(selected)
    return selected, diagnostics


def selected_local_session_observation_source_identities(
    events: list[dict[str, Any]],
) -> set[tuple[str, str, str]]:
    """Projectable (source home, client, raw session) observation identities."""

    selected, _diagnostics = reduce_local_session_observation_events(events)
    identities: set[tuple[str, str, str]] = set()
    for event in selected:
        key = local_session_observation_event_key(event)
        metadata = event.get("metadata")
        source = (
            metadata.get("source_namespace_fingerprint")
            if isinstance(metadata, dict)
            else None
        )
        if key is None or not isinstance(source, str) or not source:
            continue
        identities.add((source, key[0], key[1]))
    return identities


# Read-time plausibility slack for markers: installed_at may not exceed the
# event's own recording time by more than this (a marker cannot claim an
# install AFTER it was recorded — backfill only points backward; the CLI
# rejects future --installed-at at write time and this mirrors it at read
# time, so merge-imported/forged provenance-bearing events cannot classify
# sessions with an implausible boundary).
_INSTRUMENTATION_MARKER_MAX_FUTURE_SLACK_SECONDS = 300.0


def is_instrumentation_marker_event(event: dict[str, Any]) -> bool:
    """True only for PLAUSIBLE CLI-authored instrumentation markers.

    The provenance key is required: it is stamped exclusively by the service
    trust gate for the CLI marker writers and stripped from every other
    recording path, so an agent/HTTP event with the right event_type can
    never classify sessions as pre/post instrumentation. Provenance alone is
    not enough (store merges copy metadata verbatim), so the CLI writer's
    write-time invariants are re-checked here: metadata.client is a non-empty
    string of sane length, installed_at is a positive real number (bool is a
    forgery, not a time), and installed_at never exceeds the event's own
    created_at by more than the slack above.
    """

    metadata = event.get("metadata")
    if not (
        event.get("event_type") == INSTRUMENTATION_MARKER_EVENT_TYPE
        and isinstance(metadata, dict)
        and metadata.get("instrumentation_provenance") == CLI_INSTRUMENTATION_PROVENANCE
    ):
        return False
    client = metadata.get("client")
    if not (isinstance(client, str) and client.strip() and len(client) <= 80):
        return False
    installed_at = metadata.get("installed_at")
    if isinstance(installed_at, bool) or not isinstance(installed_at, (int, float)):
        return False
    if not installed_at > 0:
        return False
    created_at = event.get("created_at")
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        return False
    return float(installed_at) <= float(created_at) + _INSTRUMENTATION_MARKER_MAX_FUTURE_SLACK_SECONDS


def mark_trusted_instrumentation_marker_event(event: dict[str, Any]) -> dict[str, Any]:
    recorded = dict(event)
    metadata = recorded.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata["instrumentation_provenance"] = CLI_INSTRUMENTATION_PROVENANCE
    metadata.pop("reserved_instrumentation_provenance_stripped", None)
    recorded["metadata"] = metadata
    return recorded


def strip_untrusted_instrumentation_marker_metadata(event: dict[str, Any]) -> dict[str, Any]:
    recorded = dict(event)
    metadata = recorded.get("metadata")
    if not isinstance(metadata, dict):
        return recorded
    sanitized = dict(metadata)
    if "instrumentation_provenance" in sanitized:
        sanitized.pop("instrumentation_provenance", None)
        # frozen metadata key (pre-rename tombstone vocabulary, stored forever).
        sanitized["reserved_instrumentation_provenance_stripped"] = True
    recorded["metadata"] = sanitized
    return recorded
