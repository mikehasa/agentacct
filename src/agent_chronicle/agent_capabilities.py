"""Static, evidence-backed capability manifest for coding-agent adapters.

Runtime source discovery and ingestion health answer whether data is present on
this machine right now.  This module answers a different question: which
bounded integration paths agentacct implements, how they are activated, and
what evidence verifies each path.  It intentionally has no whole-client
``supported`` boolean.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any, Mapping


SCHEMA_VERSION = "agent-chronicle.agent-capabilities.v1"
CAPABILITY_NAMES = (
    "session_discovery",
    "usage_import",
    "mechanical_capture",
    "mcp_semantics",
    "model_attribution",
    "cache_read",
    "cache_write",
    "automatic_install",
)
CAPABILITY_STATES = frozenset({"unavailable", "experimental", "verified_partial", "verified"})
ACTIVATION_MODES = frozenset(
    {"none", "manual_manifest", "manual_profile", "opt_in_project", "one_command_project"}
)
VERIFICATION_LEVELS = frozenset({"none", "synthetic_fixture", "real_fixture", "live_smoke"})
REAL_VERIFICATION_LEVELS = frozenset({"real_fixture", "live_smoke"})
USAGE_BASES = frozenset({"client_reported", "unknown"})
COST_BASES = frozenset({"client_reported", "estimated_from_tokens", "unknown"})
STABILITY_LEVELS = frozenset(
    {
        "not_verified",
        "fixture_only",
        "single_machine_live_observation",
        "dated_mcp_smoke_only",
        "multi_version_verified",
    }
)
ROADMAP_PHASES = frozenset({"core", "phase_1", "phase_2"})
ZERO_USAGE_STATES = frozenset({"verified", "manual_only", "unavailable"})
NAMESPACE_HARDENING_STATES = frozenset(
    {"namespaced_fail_closed", "not_hardened", "capture_identity_only", "not_applicable"}
)
_CAPABILITY_KEYS = frozenset(
    {"state", "scope", "activation", "verification", "limitations", "usage_basis", "cost_basis"}
)
_CLIENT_KEYS = frozenset(
    {
        "client",
        "display_name",
        "roadmap_phase",
        "source_formats",
        "session_scope",
        "zero_usage_observation",
        "namespace_hardening",
        "verified_stability",
        "capabilities",
        "limitations",
    }
)


def _verification(
    level: str,
    *,
    verified_at: str | None = None,
    client_versions: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "level": level,
        "verified_at": verified_at,
        "client_versions": list(client_versions),
        "evidence_refs": list(evidence_refs),
    }


def _stability(
    level: str,
    *,
    verified_at: str | None = None,
    client_versions: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "level": level,
        "verified_at": verified_at,
        "client_versions": list(client_versions),
        "evidence_refs": list(evidence_refs),
        "limitations": list(limitations),
    }


def _capability(
    state: str,
    scope: str,
    *,
    activation: str,
    verification: Mapping[str, Any] | None = None,
    limitations: tuple[str, ...] = (),
    usage_basis: str = "unknown",
    cost_basis: str = "unknown",
) -> dict[str, Any]:
    return {
        "state": state,
        "scope": scope,
        "activation": activation,
        "verification": dict(verification or _verification("none")),
        "limitations": list(limitations),
        "usage_basis": usage_basis,
        "cost_basis": cost_basis,
    }


def _unavailable(scope: str) -> dict[str, Any]:
    return _capability("unavailable", scope, activation="none")


_P5_LIVE = _verification(
    "live_smoke",
    verified_at="2026-07-17",
    evidence_refs=("docs/adapter-capability-evidence.md#2026-07-17-local-import-and-dashboard-observation",),
)
_CORE_MCP_LIVE = _verification(
    "live_smoke",
    verified_at="2026-07-02",
    evidence_refs=("docs/live-mcp-client-smoke-results.md#2026-07-02-semantic-context-dogfood-result",),
)
_SMALL_CLIENT_MCP_LIVE = _verification(
    "live_smoke",
    verified_at="2026-06-28",
    evidence_refs=("docs/coding-agent-integrations.md#maintainer-real-client-smoke-results",),
)
_CAPTURE_FIXTURE = _verification(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=("tests/test_capture_adapters.py::test_versioned_fixtures_normalize_to_expected_canonical_events",),
)
_CLAUDE_USAGE_FIXTURE = _verification(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_claude_code_usage_sums_assistant_usage_without_transcript",
    ),
)
_CLAUDE_ONBOARD_FIXTURE = _verification(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_activation_cli.py::test_fresh_claude_hook_is_verified_before_ready",
    ),
)
_CODEX_USAGE_FIXTURE = _verification(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_codex_usage_reads_non_cached_input_and_cache_metadata",
    ),
)
_CODEX_CACHE_WRITE_FIXTURE = _verification(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_codex_usage_uses_row_level_cache_write_capability",
    ),
)
_HERMES_USAGE_FIXTURE = _verification(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_hermes_usage_reads_state_db_sessions_and_client_cost",
    ),
)
_HERMES_SESSION_FIXTURE = _verification(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_hermes_diagnostics_count_prelimit_rows_and_observe_zero_usage",
        "tests/test_client_usage.py::test_hermes_multiple_env_homes_fail_closed_until_explicit_selection",
    ),
)
_OPENCODE_USAGE_FIXTURE = _verification(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_opencode_usage_reads_json_event_stream_tokens_and_cost",
    ),
)
_OPENCLAW_USAGE_FIXTURE = _verification(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_openclaw_usage_reads_jsonl_tokens_and_cost",
    ),
)
_CURSOR_LIVE = _verification(
    "live_smoke",
    verified_at="2026-07-17",
    client_versions=("3.9.16",),
    evidence_refs=(
        "docs/adapter-capability-evidence.md#2026-07-17-cursor-3916-primary-state-observation",
    ),
)


_CLIENTS: tuple[dict[str, Any], ...] = (
    {
        "client": "claude-code",
        "display_name": "Claude Code",
        "roadmap_phase": "core",
        "source_formats": ["projects JSONL"],
        "session_scope": "Root and child transcripts, including sessions with no token row yet.",
        "zero_usage_observation": "verified",
        "namespace_hardening": "namespaced_fail_closed",
        "verified_stability": _stability(
            "single_machine_live_observation",
            verified_at="2026-07-17",
            evidence_refs=("docs/adapter-capability-evidence.md#2026-07-17-local-import-and-dashboard-observation",),
            limitations=("One live machine plus deterministic tests; multi-version stability is not claimed.",),
        ),
        "capabilities": {
            "session_discovery": _capability(
                "verified_partial",
                "Local projects JSONL root and child transcript identities, including zero-token observations.",
                activation="opt_in_project",
                verification=_P5_LIVE,
                limitations=("Only declared local Claude Code stores are scanned.",),
            ),
            "usage_import": _capability(
                "verified_partial",
                "Client-reported input and output token totals from local assistant-message rows.",
                activation="opt_in_project",
                verification=_P5_LIVE,
                limitations=("Client-reported usage is not provider billing.",),
                usage_basis="client_reported",
            ),
            "mechanical_capture": _capability(
                "experimental",
                "Metadata-only Evidence v2 normalizer; the separate legacy bridge supplies join context.",
                activation="manual_manifest",
                verification=_CAPTURE_FIXTURE,
                limitations=("Generic Evidence v2 hook manifests are not installed by onboarding.",),
            ),
            "mcp_semantics": _capability(
                "verified",
                "Project MCP config plus semantic sections, events, checks, and explicit client context.",
                activation="one_command_project",
                verification=_CORE_MCP_LIVE,
                limitations=("Semantic reports do not prove token usage or cost.",),
            ),
            "model_attribution": _capability(
                "experimental",
                "Model lanes read from assistant usage messages within each transcript.",
                activation="opt_in_project",
                verification=_CLAUDE_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; unknown or missing client model fields remain unattributed.",),
            ),
            "cache_read": _capability(
                "experimental",
                "Cache-read tokens when the client reports the field.",
                activation="opt_in_project",
                verification=_CLAUDE_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; missing fields remain unknown, never inferred as zero.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability(
                "experimental",
                "Cache-creation tokens, including reported 5-minute and 1-hour splits.",
                activation="opt_in_project",
                verification=_CLAUDE_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; missing fields remain unknown, never inferred as zero.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _capability(
                "experimental",
                "`agentacct onboard --agent claude-code` writes project MCP config, installs the context/directive hook bridge, merges hooks and env into `.claude/settings.local.json`, and verifies the bridge before marking recording ready.",
                activation="one_command_project",
                verification=_CLAUDE_ONBOARD_FIXTURE,
                limitations=(
                    "`init --write-mcp` writes MCP config and instructions only; `setup mcp --write` writes MCP config only; `hooks claude-code install` writes the wrapper/example but does not by itself activate settings.",
                    "Conflicting `.claude/settings.local.json` env values or object shapes fail closed and require manual resolution.",
                    "No live end-to-end one-command onboarding smoke has been recorded since `onboard` was added.",
                ),
            ),
        },
        "limitations": ["Exact work attribution still requires client-authored identifiers on the recording call."],
    },
    {
        "client": "codex",
        "display_name": "Codex",
        "roadmap_phase": "core",
        "source_formats": ["state_5.sqlite", "rollout JSONL"],
        "session_scope": "State database and rollout-only root/child sessions, including zero-token observations.",
        "zero_usage_observation": "verified",
        "namespace_hardening": "namespaced_fail_closed",
        "verified_stability": _stability(
            "single_machine_live_observation",
            verified_at="2026-07-17",
            evidence_refs=("docs/adapter-capability-evidence.md#2026-07-17-local-import-and-dashboard-observation",),
            limitations=("One live machine plus deterministic tests; multi-version stability is not claimed.",),
        ),
        "capabilities": {
            "session_discovery": _capability(
                "verified_partial",
                "State database and rollout JSONL identities, including rollout-only and zero-token sessions.",
                activation="opt_in_project",
                verification=_P5_LIVE,
                limitations=("Only declared local Codex stores are scanned.",),
            ),
            "usage_import": _capability(
                "verified_partial",
                "Rollout token-count events with a conservative SQLite fallback.",
                activation="opt_in_project",
                verification=_P5_LIVE,
                limitations=("Replay-like descendant rows can be held as non-additive instead of counted.",),
                usage_basis="client_reported",
            ),
            "mechanical_capture": _capability(
                "experimental",
                "Metadata-only Evidence v2 normalizer for lifecycle, tool, and check payloads.",
                activation="manual_manifest",
                verification=_CAPTURE_FIXTURE,
                limitations=("No native hook is installed by onboarding.",),
            ),
            "mcp_semantics": _capability(
                "verified",
                "Project MCP config and client-log-evidenced semantic reporting.",
                activation="one_command_project",
                verification=_CORE_MCP_LIVE,
                limitations=("Client-log joins are high confidence, never exact.",),
            ),
            "model_attribution": _capability(
                "experimental",
                "Session or rollout model metadata, with provenance-labeled exact-parent inheritance only.",
                activation="opt_in_project",
                limitations=("No field-level live evidence is recorded; internal workflow labels are not accepted as models.",),
            ),
            "cache_read": _capability(
                "experimental",
                "Cached-input tokens when rollout rows report them.",
                activation="opt_in_project",
                verification=_CODEX_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; SQLite-only fallback keeps the split unknown.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability(
                "experimental",
                "Cache-write tokens only on rollout rows that expose the field.",
                activation="opt_in_project",
                verification=_CODEX_CACHE_WRITE_FIXTURE,
                limitations=("Only a synthetic field-presence fixture proves this path; current live rows did not report it.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _capability(
                "verified_partial",
                "agentacct can write project MCP config and workflow instructions.",
                activation="one_command_project",
                verification=_CORE_MCP_LIVE,
                limitations=("The generic mechanical hook remains manual and some Codex builds ignore project-local config.",),
            ),
        },
        "limitations": ["Usage and semantic work remain separate unless client-log evidence proves the join."],
    },
    {
        "client": "hermes",
        "display_name": "Hermes",
        "roadmap_phase": "phase_1",
        "source_formats": ["state.db sessions"],
        "session_scope": "Modeled state.db session rows, including rows with no positive usage yet.",
        "zero_usage_observation": "verified",
        "namespace_hardening": "namespaced_fail_closed",
        "verified_stability": _stability(
            "single_machine_live_observation",
            verified_at="2026-07-17",
            evidence_refs=("docs/adapter-capability-evidence.md#2026-07-17-local-import-and-dashboard-observation",),
            limitations=("Usage-bearing rows worked on one live store; zero-token observation and multi-home fail-closed behavior have deterministic fixture evidence only.",),
        ),
        "capabilities": {
            "session_discovery": _capability(
                "experimental",
                "Modeled rows in one selected Hermes state database, including zero-token session observations.",
                activation="opt_in_project",
                verification=_HERMES_SESSION_FIXTURE,
                limitations=("Zero-token and multi-home behavior is fixture-verified; only usage-bearing rows have live evidence.",),
            ),
            "usage_import": _capability(
                "verified_partial",
                "Client-reported input and output token totals from usage-bearing state.db rows.",
                activation="opt_in_project",
                verification=_P5_LIVE,
                limitations=("Optional model/cache/cost fields have synthetic evidence only; schema drift can collapse to an empty result.",),
                usage_basis="client_reported",
            ),
            "mechanical_capture": _unavailable("No Hermes mechanical hook or plugin adapter is implemented."),
            "mcp_semantics": _capability(
                "verified_partial",
                "Manual Hermes profile registration can expose agentacct semantic tools.",
                activation="manual_profile",
                verification=_SMALL_CLIENT_MCP_LIVE,
                limitations=("The dated smoke proves MCP calls, not usage import stability.",),
            ),
            "model_attribution": _capability(
                "experimental",
                "Model and billing provider columns on usage-bearing session rows.",
                activation="opt_in_project",
                verification=_HERMES_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; zero-token rows expose an observed model without claiming usage.",),
            ),
            "cache_read": _capability(
                "experimental",
                "cache_read_tokens when present in the expected state.db schema.",
                activation="opt_in_project",
                verification=_HERMES_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; older schemas are not degraded field-by-field.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability(
                "experimental",
                "cache_write_tokens when present in the expected state.db schema.",
                activation="opt_in_project",
                verification=_HERMES_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; the live store did not provide positive cache-write proof.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _unavailable(
                "agentacct can render the setup command but does not write the active Hermes profile."
            ),
        },
        "limitations": ["Remain provisional until schema-drift recovery and zero-token observation have live evidence."],
    },
    {
        "client": "opencode",
        "display_name": "OpenCode",
        "roadmap_phase": "phase_1",
        "source_formats": ["captured/exported JSON event stream"],
        "session_scope": "Usage-bearing step-finish exports only.",
        "zero_usage_observation": "unavailable",
        "namespace_hardening": "not_hardened",
        "verified_stability": _stability(
            "dated_mcp_smoke_only",
            verified_at="2026-06-28",
            client_versions=("1.17.11",),
            evidence_refs=("docs/coding-agent-integrations.md#maintainer-real-client-smoke-results",),
            limitations=("Only the MCP lane had a real-client smoke; usage parsing remains synthetic-fixture only.",),
        ),
        "capabilities": {
            "session_discovery": _capability(
                "experimental",
                "Session IDs found in captured/exported JSON step-finish streams.",
                activation="opt_in_project",
                verification=_OPENCODE_USAGE_FIXTURE,
                limitations=("The official SQLite store is detected but not imported; zero-token sessions are absent.",),
            ),
            "usage_import": _capability(
                "experimental",
                "Input, output, reasoning, cache, and client cost from exported step-finish rows.",
                activation="opt_in_project",
                verification=_OPENCODE_USAGE_FIXTURE,
                limitations=("Synthetic happy-path fixture only; malformed/schema-drift diagnostics are incomplete.",),
                usage_basis="client_reported",
                cost_basis="client_reported",
            ),
            "mechanical_capture": _unavailable("No realtime OpenCode plugin adapter is implemented."),
            "mcp_semantics": _capability(
                "verified_partial",
                "Manual OpenCode user-config registration can expose agentacct semantic tools.",
                activation="manual_profile",
                verification=_SMALL_CLIENT_MCP_LIVE,
                limitations=("The dated DeepSeek smoke does not verify OpenAI or Anthropic paths or usage import.",),
            ),
            "model_attribution": _capability(
                "experimental",
                "First model-like field found in a captured JSON export.",
                activation="opt_in_project",
                limitations=("No fixture asserts model extraction; model is not bound to each step-finish row.",),
            ),
            "cache_read": _capability(
                "experimental",
                "tokens.cache.read when the export reports it.",
                activation="opt_in_project",
                verification=_OPENCODE_USAGE_FIXTURE,
                limitations=("Synthetic fixture only; missing remains unknown.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability(
                "experimental",
                "tokens.cache.write when the export reports it.",
                activation="opt_in_project",
                verification=_OPENCODE_USAGE_FIXTURE,
                limitations=("Synthetic fixture only; missing remains unknown.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _unavailable(
                "agentacct renders the setup command but does not write OpenCode user config or install a plugin."
            ),
        },
        "limitations": ["Remain experimental until real fixtures, namespace hardening, and official SQLite parsing exist."],
    },
    {
        "client": "openclaw",
        "display_name": "OpenClaw",
        "roadmap_phase": "phase_1",
        "source_formats": ["assistant-message JSONL"],
        "session_scope": "Usage-bearing assistant rows in session log files only.",
        "zero_usage_observation": "unavailable",
        "namespace_hardening": "not_hardened",
        "verified_stability": _stability(
            "dated_mcp_smoke_only",
            verified_at="2026-06-28",
            client_versions=("2026.6.10",),
            evidence_refs=("docs/coding-agent-integrations.md#maintainer-real-client-smoke-results",),
            limitations=("Only the MCP lane had a real-client smoke; usage parsing remains synthetic-fixture only.",),
        ),
        "capabilities": {
            "session_discovery": _capability(
                "experimental",
                "Session IDs derived from JSONL filenames that contain usage-bearing assistant rows.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("sessions.json routing and zero-token sessions are not integrated.",),
            ),
            "usage_import": _capability(
                "experimental",
                "Input, output, cache, and optional client cost from assistant usage rows.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("Synthetic happy-path fixture only; malformed/schema-drift diagnostics are incomplete.",),
                usage_basis="client_reported",
                cost_basis="client_reported",
            ),
            "mechanical_capture": _unavailable("No typed OpenClaw plugin-hook adapter is implemented."),
            "mcp_semantics": _capability(
                "verified_partial",
                "Manual OpenClaw profile registration can expose agentacct semantic tools.",
                activation="manual_profile",
                verification=_SMALL_CLIENT_MCP_LIVE,
                limitations=("The dated DeepSeek smoke does not verify the blocked Claude CLI profile path.",),
            ),
            "model_attribution": _capability(
                "experimental",
                "Model/provider snapshots or assistant-row fields within one JSONL file.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("A multi-model session is currently aggregated under the final/current model.",),
            ),
            "cache_read": _capability(
                "experimental",
                "cacheRead when an assistant usage row reports it.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("Synthetic fixture only; missing remains unknown.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability(
                "experimental",
                "cacheWrite when an assistant usage row reports it.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("Synthetic fixture only; missing remains unknown.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _unavailable(
                "agentacct renders the setup command but does not write OpenClaw profile config or install typed hooks."
            ),
        },
        "limitations": ["Remain experimental until real fixtures, namespace hardening, routing, and per-model lanes exist."],
    },
    {
        "client": "cursor",
        "display_name": "Cursor",
        "roadmap_phase": "phase_1",
        "source_formats": [
            "primary User/globalStorage/state.vscdb cursorDiskKV composerData rows",
            "manually wired hook payloads",
        ],
        "session_scope": "Metadata-only root/child composer observations; no prompt, message, title, token, or cost fields.",
        "zero_usage_observation": "verified",
        "namespace_hardening": "namespaced_fail_closed",
        "verified_stability": _stability(
            "single_machine_live_observation",
            verified_at="2026-07-17",
            client_versions=("3.9.16",),
            evidence_refs=(
                "docs/adapter-capability-evidence.md#2026-07-17-cursor-3916-primary-state-observation",
            ),
            limitations=(
                "One real local Cursor version plus deterministic fixtures; multi-version stability is not claimed.",
            ),
        ),
        "capabilities": {
            "session_discovery": _capability(
                "verified_partial",
                "Primary state.vscdb composer identities, timestamps, allowlisted model metadata, and exact child links.",
                activation="opt_in_project",
                verification=_CURSOR_LIVE,
                limitations=(
                    "Only the primary state.vscdb is read; backups and ai-tracking stores are excluded.",
                    "Active WAL, schema drift, unsafe paths, and invalid lineage fail closed.",
                    "One real Cursor 3.9.16 store was exercised; multi-version stability is not claimed.",
                ),
            ),
            "usage_import": _unavailable("No official local Cursor token importer is claimed."),
            "mechanical_capture": _capability(
                "experimental",
                "Metadata-only lifecycle, turn, tool, subagent, artifact, and machine-check normalization.",
                activation="manual_manifest",
                verification=_CAPTURE_FIXTURE,
                limitations=("The manifest is render-only and onboarding does not activate it.",),
            ),
            "mcp_semantics": _capability(
                "experimental",
                "A portable generic MCP definition may be configured manually.",
                activation="manual_profile",
                limitations=("No real Cursor MCP smoke has been recorded.",),
            ),
            "model_attribution": _capability(
                "verified_partial",
                "Allowlisted modelConfig.modelName from each composer row; missing/default values remain unknown.",
                activation="opt_in_project",
                verification=_CURSOR_LIVE,
                limitations=(
                    "No model is inferred from prompt, message, title, or subagent content.",
                    "One real Cursor 3.9.16 store was exercised; missing/default models remain unattributed.",
                ),
            ),
            "cache_read": _unavailable("Hook payload normalization does not report token usage."),
            "cache_write": _unavailable("Hook payload normalization does not report token usage."),
            "automatic_install": _unavailable("agentacct does not install Cursor hooks or client config."),
        },
        "limitations": [
            "Activity observations never fabricate token usage, cache usage, cost, titles, projects, or semantic work descriptions."
        ],
    },
    *(
        {
            "client": client,
            "display_name": display_name,
            "roadmap_phase": "phase_2",
            "source_formats": [],
            "session_scope": "No adapter implementation or verified fixture exists yet.",
            "zero_usage_observation": "unavailable",
            "namespace_hardening": "not_applicable",
            "verified_stability": _stability("not_verified"),
            "capabilities": {
                name: _unavailable("Roadmap entry only; no implementation or verification exists.")
                for name in CAPABILITY_NAMES
            },
            "limitations": ["Generic MCP availability does not upgrade this named client without a real client smoke."],
        }
        for client, display_name in (
            ("gemini-cli", "Gemini CLI"),
            ("github-copilot-cli", "GitHub Copilot CLI"),
            ("cline", "Cline"),
            ("windsurf", "Windsurf"),
            ("aider", "Aider"),
        )
    ),
)


def validate_agent_capability_manifest(manifest: Mapping[str, Any]) -> None:
    """Reject ambiguous or overclaimed capability data."""

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid agent capability schema version")
    clients = manifest.get("clients")
    if not isinstance(clients, list):
        raise ValueError("agent capability clients must be a list")
    client_ids: set[str] = set()
    for row in clients:
        if not isinstance(row, Mapping):
            raise ValueError("agent capability row must be an object")
        if set(row) != _CLIENT_KEYS:
            raise ValueError("agent capability rows must use the canonical fields")
        if "supported" in row:
            raise ValueError("whole-client supported booleans are forbidden")
        client = row.get("client")
        if not isinstance(client, str) or not client or client in client_ids:
            raise ValueError("agent capability client ids must be unique non-empty strings")
        client_ids.add(client)
        if row.get("roadmap_phase") not in ROADMAP_PHASES:
            raise ValueError(f"{client} has invalid roadmap phase")
        if row.get("zero_usage_observation") not in ZERO_USAGE_STATES:
            raise ValueError(f"{client} has invalid zero-usage observation state")
        if row.get("namespace_hardening") not in NAMESPACE_HARDENING_STATES:
            raise ValueError(f"{client} has invalid namespace hardening state")
        stability = row.get("verified_stability")
        if not isinstance(stability, Mapping) or stability.get("level") not in STABILITY_LEVELS:
            raise ValueError(f"{client} has invalid verified stability")
        if stability.get("level") != "not_verified":
            _validate_dated_evidence(stability, context=f"{client}.verified_stability")
        if stability.get("level") == "dated_mcp_smoke_only" and not stability.get("client_versions"):
            raise ValueError(f"{client} dated client smoke requires a client version")
        capabilities = row.get("capabilities")
        if not isinstance(capabilities, Mapping) or set(capabilities) != set(CAPABILITY_NAMES):
            raise ValueError(f"{client} must declare the canonical capability set")
        for name, capability in capabilities.items():
            if not isinstance(capability, Mapping):
                raise ValueError(f"{client}.{name} must be an object")
            if set(capability) != _CAPABILITY_KEYS:
                raise ValueError(f"{client}.{name} must use the canonical capability fields")
            state = capability.get("state")
            activation = capability.get("activation")
            scope = capability.get("scope")
            verification = capability.get("verification")
            usage_basis = capability.get("usage_basis")
            cost_basis = capability.get("cost_basis")
            if state not in CAPABILITY_STATES:
                raise ValueError(f"{client}.{name} has invalid state")
            if activation not in ACTIVATION_MODES:
                raise ValueError(f"{client}.{name} has invalid activation")
            if not isinstance(scope, str) or not scope.strip():
                raise ValueError(f"{client}.{name} must declare a bounded scope")
            if not isinstance(verification, Mapping) or verification.get("level") not in VERIFICATION_LEVELS:
                raise ValueError(f"{client}.{name} has invalid verification")
            if usage_basis not in USAGE_BASES or cost_basis not in COST_BASES:
                raise ValueError(f"{client}.{name} has invalid usage or cost basis")
            level = verification.get("level")
            if state in {"verified", "verified_partial"}:
                if level not in REAL_VERIFICATION_LEVELS:
                    raise ValueError(f"{client}.{name} verified state requires real evidence")
                _validate_dated_evidence(verification, context=f"{client}.{name}")
            if level == "synthetic_fixture" and state in {"verified", "verified_partial"}:
                raise ValueError(f"{client}.{name} synthetic fixtures cannot prove verified state")
            if level == "synthetic_fixture":
                refs = verification.get("evidence_refs")
                if not isinstance(refs, list) or not refs or any("::test_" not in str(ref) for ref in refs):
                    raise ValueError(f"{client}.{name} synthetic evidence must name exact tests")
            if state == "unavailable":
                if activation != "none" or level != "none":
                    raise ValueError(f"{client}.{name} unavailable state cannot claim activation or verification")
                if usage_basis != "unknown" or cost_basis != "unknown":
                    raise ValueError(f"{client}.{name} unavailable state cannot claim usage or cost basis")
            if name in {"mechanical_capture", "mcp_semantics"} and (
                usage_basis != "unknown" or cost_basis != "unknown"
            ):
                raise ValueError(f"{client}.{name} cannot claim usage or cost basis")
            if name == "usage_import" and state != "unavailable" and usage_basis != "client_reported":
                raise ValueError(f"{client}.{name} must declare client-reported usage basis")
            if name in {"cache_read", "cache_write"} and state != "unavailable" and usage_basis != "client_reported":
                raise ValueError(f"{client}.{name} must declare the cache usage basis")
            if name == "automatic_install" and state != "unavailable" and activation not in {
                "opt_in_project",
                "one_command_project",
            }:
                raise ValueError(f"{client}.{name} cannot label a manual path automatic")


def _validate_dated_evidence(evidence: Mapping[str, Any], *, context: str) -> None:
    verified_at = evidence.get("verified_at")
    refs = evidence.get("evidence_refs")
    try:
        parsed = date.fromisoformat(str(verified_at))
    except ValueError as exc:
        raise ValueError(f"{context} requires dated evidence refs") from exc
    if parsed.isoformat() != verified_at or not isinstance(refs, list) or not refs:
        raise ValueError(f"{context} requires dated evidence refs")
    if any(
        not isinstance(ref, str) or not ref or ref.startswith("/") or ".." in ref.split("/")
        for ref in refs
    ):
        raise ValueError(f"{context} evidence refs must be non-empty relative paths")


def agent_capability_manifest() -> dict[str, Any]:
    """Return a defensive copy of the canonical static manifest."""

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "last_reviewed_at": "2026-07-17",
        "support_policy": (
            "Capabilities are evaluated independently. Runtime detection and ingestion health do not imply "
            "that every integration lane is verified. Missing or conditional token fields remain unknown."
        ),
        "runtime_truth": {
            "source_detection": "/usage/sources",
            "ingestion_health": "/ingestion/health",
            "historical_evidence": "/evidence/product",
        },
        "clients": list(_CLIENTS),
    }
    validate_agent_capability_manifest(manifest)
    return deepcopy(manifest)


__all__ = [
    "ACTIVATION_MODES",
    "CAPABILITY_NAMES",
    "CAPABILITY_STATES",
    "SCHEMA_VERSION",
    "VERIFICATION_LEVELS",
    "agent_capability_manifest",
    "validate_agent_capability_manifest",
]
