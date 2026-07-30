"""Static, evidence-backed capability manifest for coding-agent adapters.

This is the single source of truth for "which coding-agent integrations
agentacct supports, how strongly, and what evidence proves each one."  It rates
every supported agent across eight capability lanes, and it deliberately has no
whole-client ``supported`` boolean -- each lane is rated independently, because
a client can be strong in one (e.g. usage) and weak in another (e.g. mechanical
capture).

This is the *static* half of agentacct's integration story: what it can do with
each agent in principle.  The *dynamic* half -- is a given agent's data actually
present on THIS machine right now? -- comes from runtime source discovery
(``discover_usage_sources``) and ingestion health (``IngestionHealthStore``).
``agent_capability_manifest()`` feeds the ``agentacct capabilities agents`` CLI
command (``cli.py``), the dashboard's ``/capabilities/agents`` endpoint
(``api.py``), and the HTML matrix renderer (``evidence_html.py``); it always
returns a validated deep copy.  The evidence behind each rating lives in
``docs/adapter-capability-evidence.md``.

What's inside
-------------
1. Vocabulary (the frozensets below): the controlled words every entry uses.
2. Factories (``_verification_record``, ``_stability_record``, ``_capability_record``): small dict
   builders that keep the entries consistent and terse.
3. ``_CLIENTS``: the table -- one block per coding agent, each declaring all
   eight capabilities plus client-level metadata.  This is most of the file.
4. ``validate_agent_capability_manifest``: the integrity rules -- rejects any
   entry that overclaims (e.g. "verified" without real, dated evidence).
5. ``agent_capability_manifest``: returns a validated deep copy for callers.

Vocabulary (plain-English)
--------------------------
The 8 capability lanes every client is rated on:
  session_discovery  find the agent's local session logs
  usage_import       read token totals from those logs
  mechanical_capture observe tool/lifecycle events via hooks
  mcp_semantics      record work meaning over MCP (sections, checks)
  model_attribution  attribute tokens to a model
  cache_read         read cache-hit tokens
  cache_write        read cache-creation tokens
  automatic_install  install the integration with one command

State -- how real a lane is:
  unavailable        not implemented
  experimental       implemented, but only tested with synthetic data
  verified_partial   works, with real evidence but narrow scope
  verified           works, with real evidence

Verification level -- what proves the state:
  none               no evidence
  synthetic_fixture  a unit-test fixture (can prove "experimental", never "verified")
  real_fixture       evidence captured from a real local run
  live_smoke         observed on a live machine on a dated day

Activation -- how a user turns a lane on:
  none | manual_manifest | manual_profile | opt_in_project | one_command_project

The honesty rule: a "verified*" state requires real, dated evidence, and a
synthetic fixture can never prove a verified state.  The validator enforces
this, so the published matrix cannot overclaim.
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


def _verification_record(
    level: str,
    *,
    verified_at: str | None = None,
    client_versions: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a verification-evidence record: the level, date, client versions, and doc refs."""
    return {
        "level": level,
        "verified_at": verified_at,
        "client_versions": list(client_versions),
        "evidence_refs": list(evidence_refs),
    }


def _stability_record(
    level: str,
    *,
    verified_at: str | None = None,
    client_versions: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a client-level stability record: how broadly the client has been observed."""
    return {
        "level": level,
        "verified_at": verified_at,
        "client_versions": list(client_versions),
        "evidence_refs": list(evidence_refs),
        "limitations": list(limitations),
    }


def _capability_record(
    state: str,
    scope: str,
    *,
    activation: str,
    verification: Mapping[str, Any] | None = None,
    limitations: tuple[str, ...] = (),
    usage_basis: str = "unknown",
    cost_basis: str = "unknown",
) -> dict[str, Any]:
    """Build one capability cell: state, bounded scope, activation, evidence, and usage/cost basis."""
    return {
        "state": state,
        "scope": scope,
        "activation": activation,
        "verification": dict(verification or _verification_record("none")),
        "limitations": list(limitations),
        "usage_basis": usage_basis,
        "cost_basis": cost_basis,
    }


def _unavailable_capability(scope: str) -> dict[str, Any]:
    """Build an unavailable capability cell: not implemented, no activation, no evidence."""
    return _capability_record("unavailable", scope, activation="none")


_LOCAL_IMPORT_LIVE = _verification_record(
    "live_smoke",
    verified_at="2026-07-17",
    evidence_refs=("docs/adapter-capability-evidence.md#2026-07-17-local-import-and-dashboard-observation",),
)
_CORE_MCP_LIVE = _verification_record(
    "live_smoke",
    verified_at="2026-07-02",
    evidence_refs=("docs/live-mcp-client-smoke-results.md#2026-07-02-semantic-context-dogfood-result",),
)
_SMALL_CLIENT_MCP_LIVE = _verification_record(
    "live_smoke",
    verified_at="2026-06-28",
    evidence_refs=("docs/coding-agent-integrations.md#maintainer-real-client-smoke-results",),
)
_CAPTURE_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=("tests/test_capture_adapters.py::test_versioned_fixtures_normalize_to_expected_canonical_events",),
)
_CLAUDE_USAGE_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_claude_code_usage_sums_assistant_usage_without_transcript",
    ),
)
_CLAUDE_ONBOARD_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_activation_cli.py::test_fresh_claude_hook_is_verified_before_ready",
    ),
)
_CODEX_USAGE_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_codex_usage_reads_non_cached_input_and_cache_metadata",
    ),
)
_CODEX_CACHE_WRITE_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_codex_usage_uses_row_level_cache_write_capability",
    ),
)
_HERMES_USAGE_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_hermes_usage_reads_state_db_sessions_and_client_cost",
    ),
)
_HERMES_SESSION_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_hermes_diagnostics_count_prelimit_rows_and_observe_zero_usage",
        "tests/test_client_usage.py::test_hermes_multiple_env_homes_fail_closed_until_explicit_selection",
    ),
)
_OPENCODE_USAGE_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_opencode_usage_reads_json_event_stream_tokens_and_cost",
    ),
)
_OPENCODE_DB_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-29",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_opencode_usage_reads_native_session_db",
        "tests/test_client_usage.py::test_discover_opencode_usage_recomputes_cost_from_tokens_when_stored_zero",
        "tests/test_client_usage.py::test_discover_opencode_usage_presence_flags_track_schema_columns",
    ),
)
_OPENCLAW_USAGE_FIXTURE = _verification_record(
    "synthetic_fixture",
    verified_at="2026-07-17",
    evidence_refs=(
        "tests/test_client_usage.py::test_discover_openclaw_usage_reads_jsonl_tokens_and_cost",
    ),
)
_CURSOR_LIVE = _verification_record(
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
        "verified_stability": _stability_record(
            "single_machine_live_observation",
            verified_at="2026-07-17",
            evidence_refs=("docs/adapter-capability-evidence.md#2026-07-17-local-import-and-dashboard-observation",),
            limitations=("One live machine plus deterministic tests; multi-version stability is not claimed.",),
        ),
        "capabilities": {
            "session_discovery": _capability_record(
                "verified_partial",
                "Local projects JSONL root and child transcript identities, including zero-token observations.",
                activation="opt_in_project",
                verification=_LOCAL_IMPORT_LIVE,
                limitations=("Only declared local Claude Code stores are scanned.",),
            ),
            "usage_import": _capability_record(
                "verified_partial",
                "Client-reported input and output token totals from local assistant-message rows.",
                activation="opt_in_project",
                verification=_LOCAL_IMPORT_LIVE,
                limitations=("Client-reported usage is not provider billing.",),
                usage_basis="client_reported",
            ),
            "mechanical_capture": _capability_record(
                "experimental",
                "Metadata-only Evidence v2 normalizer; the separate legacy bridge supplies join context.",
                activation="manual_manifest",
                verification=_CAPTURE_FIXTURE,
                limitations=("Generic Evidence v2 hook manifests are not installed by onboarding.",),
            ),
            "mcp_semantics": _capability_record(
                "verified",
                "Project MCP config plus semantic sections, events, checks, and explicit client context.",
                activation="one_command_project",
                verification=_CORE_MCP_LIVE,
                limitations=("Semantic reports do not prove token usage or cost.",),
            ),
            "model_attribution": _capability_record(
                "experimental",
                "Model lanes read from assistant usage messages within each transcript.",
                activation="opt_in_project",
                verification=_CLAUDE_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; unknown or missing client model fields remain unattributed.",),
            ),
            "cache_read": _capability_record(
                "experimental",
                "Cache-read tokens when the client reports the field.",
                activation="opt_in_project",
                verification=_CLAUDE_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; missing fields remain unknown, never inferred as zero.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability_record(
                "experimental",
                "Cache-creation tokens, including reported 5-minute and 1-hour splits.",
                activation="opt_in_project",
                verification=_CLAUDE_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; missing fields remain unknown, never inferred as zero.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _capability_record(
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
        "verified_stability": _stability_record(
            "single_machine_live_observation",
            verified_at="2026-07-17",
            evidence_refs=("docs/adapter-capability-evidence.md#2026-07-17-local-import-and-dashboard-observation",),
            limitations=("One live machine plus deterministic tests; multi-version stability is not claimed.",),
        ),
        "capabilities": {
            "session_discovery": _capability_record(
                "verified_partial",
                "State database and rollout JSONL identities, including rollout-only and zero-token sessions.",
                activation="opt_in_project",
                verification=_LOCAL_IMPORT_LIVE,
                limitations=("Only declared local Codex stores are scanned.",),
            ),
            "usage_import": _capability_record(
                "verified_partial",
                "Rollout token-count events with a conservative SQLite fallback.",
                activation="opt_in_project",
                verification=_LOCAL_IMPORT_LIVE,
                limitations=("Replay-like descendant rows can be held as non-additive instead of counted.",),
                usage_basis="client_reported",
            ),
            "mechanical_capture": _capability_record(
                "experimental",
                "Metadata-only Evidence v2 normalizer for lifecycle, tool, and check payloads.",
                activation="manual_manifest",
                verification=_CAPTURE_FIXTURE,
                limitations=("No native hook is installed by onboarding.",),
            ),
            "mcp_semantics": _capability_record(
                "verified",
                "Project MCP config and client-log-evidenced semantic reporting.",
                activation="one_command_project",
                verification=_CORE_MCP_LIVE,
                limitations=("Client-log joins are high confidence, never exact.",),
            ),
            "model_attribution": _capability_record(
                "experimental",
                "Session or rollout model metadata, with provenance-labeled exact-parent inheritance only.",
                activation="opt_in_project",
                limitations=("No field-level live evidence is recorded; internal workflow labels are not accepted as models.",),
            ),
            "cache_read": _capability_record(
                "experimental",
                "Cached-input tokens when rollout rows report them.",
                activation="opt_in_project",
                verification=_CODEX_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; SQLite-only fallback keeps the split unknown.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability_record(
                "experimental",
                "Cache-write tokens only on rollout rows that expose the field.",
                activation="opt_in_project",
                verification=_CODEX_CACHE_WRITE_FIXTURE,
                limitations=("Only a synthetic field-presence fixture proves this path; current live rows did not report it.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _capability_record(
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
        "verified_stability": _stability_record(
            "single_machine_live_observation",
            verified_at="2026-07-17",
            evidence_refs=("docs/adapter-capability-evidence.md#2026-07-17-local-import-and-dashboard-observation",),
            limitations=("Usage-bearing rows worked on one live store; zero-token observation and multi-home fail-closed behavior have deterministic fixture evidence only.",),
        ),
        "capabilities": {
            "session_discovery": _capability_record(
                "experimental",
                "Modeled rows in one selected Hermes state database, including zero-token session observations.",
                activation="opt_in_project",
                verification=_HERMES_SESSION_FIXTURE,
                limitations=("Zero-token and multi-home behavior is fixture-verified; only usage-bearing rows have live evidence.",),
            ),
            "usage_import": _capability_record(
                "verified_partial",
                "Client-reported input and output token totals from usage-bearing state.db rows.",
                activation="opt_in_project",
                verification=_LOCAL_IMPORT_LIVE,
                limitations=("Optional model/cache/cost fields have synthetic evidence only; schema drift can collapse to an empty result.",),
                usage_basis="client_reported",
            ),
            "mechanical_capture": _unavailable_capability("No Hermes mechanical hook or plugin adapter is implemented."),
            "mcp_semantics": _capability_record(
                "verified_partial",
                "Manual Hermes profile registration can expose agentacct semantic tools.",
                activation="manual_profile",
                verification=_SMALL_CLIENT_MCP_LIVE,
                limitations=("The dated smoke proves MCP calls, not usage import stability.",),
            ),
            "model_attribution": _capability_record(
                "experimental",
                "Model and billing provider columns on usage-bearing session rows.",
                activation="opt_in_project",
                verification=_HERMES_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; zero-token rows expose an observed model without claiming usage.",),
            ),
            "cache_read": _capability_record(
                "experimental",
                "cache_read_tokens when present in the expected state.db schema.",
                activation="opt_in_project",
                verification=_HERMES_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; older schemas are not degraded field-by-field.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability_record(
                "experimental",
                "cache_write_tokens when present in the expected state.db schema.",
                activation="opt_in_project",
                verification=_HERMES_USAGE_FIXTURE,
                limitations=("Field-level evidence is synthetic; the live store did not provide positive cache-write proof.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _unavailable_capability(
                "agentacct can render the setup command but does not write the active Hermes profile."
            ),
        },
        "limitations": ["Remain provisional until schema-drift recovery and zero-token observation have live evidence."],
    },
    {
        "client": "opencode",
        "display_name": "OpenCode",
        "roadmap_phase": "phase_1",
        "source_formats": ["native opencode.db SQLite session rollup", "captured/exported JSON event stream"],
        "session_scope": "Usage-bearing session rollups (SQLite) or step-finish exports.",
        "zero_usage_observation": "unavailable",
        "namespace_hardening": "not_hardened",
        "verified_stability": _stability_record(
            "dated_mcp_smoke_only",
            verified_at="2026-06-28",
            client_versions=("1.17.11",),
            evidence_refs=("docs/coding-agent-integrations.md#maintainer-real-client-smoke-results",),
            limitations=("Only the MCP lane had a real-client smoke; usage parsing remains synthetic-fixture only.",),
        ),
        "capabilities": {
            "session_discovery": _capability_record(
                "experimental",
                "Session IDs from the native opencode.db session rollup, or captured/exported JSON step-finish streams as a fallback.",
                activation="opt_in_project",
                verification=_OPENCODE_DB_FIXTURE,
                limitations=("Only usage-bearing sessions are emitted; zero-token sessions are absent.",),
            ),
            "usage_import": _capability_record(
                "experimental",
                "Per-session input, output, reasoning, and cache token totals from the native SQLite session rollup (JSON export fallback); cost recomputed from tokens when the store records none.",
                activation="opt_in_project",
                verification=_OPENCODE_DB_FIXTURE,
                limitations=(
                    "Synthetic-fixture only; per-message granularity is not imported (session totals only).",
                    "OpenCode usually stores cost 0, so cost is typically estimated_from_tokens; a nonzero stored cost is kept as client_reported.",
                ),
                usage_basis="client_reported",
                cost_basis="estimated_from_tokens",
            ),
            "mechanical_capture": _unavailable_capability("No realtime OpenCode plugin adapter is implemented."),
            "mcp_semantics": _capability_record(
                "verified_partial",
                "Manual OpenCode user-config registration can expose agentacct semantic tools.",
                activation="manual_profile",
                verification=_SMALL_CLIENT_MCP_LIVE,
                limitations=("The dated DeepSeek smoke does not verify OpenAI or Anthropic paths or usage import.",),
            ),
            "model_attribution": _capability_record(
                "experimental",
                "Model id and provider id parsed from the session rollup's model JSON (JSON-export model field as fallback).",
                activation="opt_in_project",
                verification=_OPENCODE_DB_FIXTURE,
                limitations=("Per-message model binding is not imported; the session rollup carries one model per session.",),
            ),
            "cache_read": _capability_record(
                "experimental",
                "tokens.cache.read when the export reports it.",
                activation="opt_in_project",
                verification=_OPENCODE_USAGE_FIXTURE,
                limitations=("Synthetic fixture only; missing remains unknown.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability_record(
                "experimental",
                "tokens.cache.write when the export reports it.",
                activation="opt_in_project",
                verification=_OPENCODE_USAGE_FIXTURE,
                limitations=("Synthetic fixture only; missing remains unknown.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _unavailable_capability(
                "agentacct renders the setup command but does not write OpenCode user config or install a plugin."
            ),
        },
        "limitations": ["Remain experimental until real-client fixtures, namespace hardening, and per-message SQLite granularity exist."],
    },
    {
        "client": "openclaw",
        "display_name": "OpenClaw",
        "roadmap_phase": "phase_1",
        "source_formats": ["assistant-message JSONL"],
        "session_scope": "Usage-bearing assistant rows in session log files only.",
        "zero_usage_observation": "unavailable",
        "namespace_hardening": "not_hardened",
        "verified_stability": _stability_record(
            "dated_mcp_smoke_only",
            verified_at="2026-06-28",
            client_versions=("2026.6.10",),
            evidence_refs=("docs/coding-agent-integrations.md#maintainer-real-client-smoke-results",),
            limitations=("Only the MCP lane had a real-client smoke; usage parsing remains synthetic-fixture only.",),
        ),
        "capabilities": {
            "session_discovery": _capability_record(
                "experimental",
                "Session IDs derived from JSONL filenames that contain usage-bearing assistant rows.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("sessions.json routing and zero-token sessions are not integrated.",),
            ),
            "usage_import": _capability_record(
                "experimental",
                "Input, output, cache, and optional client cost from assistant usage rows.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("Synthetic happy-path fixture only; malformed/schema-drift diagnostics are incomplete.",),
                usage_basis="client_reported",
                cost_basis="client_reported",
            ),
            "mechanical_capture": _unavailable_capability("No typed OpenClaw plugin-hook adapter is implemented."),
            "mcp_semantics": _capability_record(
                "verified_partial",
                "Manual OpenClaw profile registration can expose agentacct semantic tools.",
                activation="manual_profile",
                verification=_SMALL_CLIENT_MCP_LIVE,
                limitations=("The dated DeepSeek smoke does not verify the blocked Claude CLI profile path.",),
            ),
            "model_attribution": _capability_record(
                "experimental",
                "Model/provider snapshots or assistant-row fields within one JSONL file.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("A multi-model session is currently aggregated under the final/current model.",),
            ),
            "cache_read": _capability_record(
                "experimental",
                "cacheRead when an assistant usage row reports it.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("Synthetic fixture only; missing remains unknown.",),
                usage_basis="client_reported",
            ),
            "cache_write": _capability_record(
                "experimental",
                "cacheWrite when an assistant usage row reports it.",
                activation="opt_in_project",
                verification=_OPENCLAW_USAGE_FIXTURE,
                limitations=("Synthetic fixture only; missing remains unknown.",),
                usage_basis="client_reported",
            ),
            "automatic_install": _unavailable_capability(
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
        "verified_stability": _stability_record(
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
            "session_discovery": _capability_record(
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
            "usage_import": _unavailable_capability("No official local Cursor token importer is claimed."),
            "mechanical_capture": _capability_record(
                "experimental",
                "Metadata-only lifecycle, turn, tool, subagent, artifact, and machine-check normalization.",
                activation="manual_manifest",
                verification=_CAPTURE_FIXTURE,
                limitations=("The manifest is render-only and onboarding does not activate it.",),
            ),
            "mcp_semantics": _capability_record(
                "experimental",
                "A portable generic MCP definition may be configured manually.",
                activation="manual_profile",
                limitations=("No real Cursor MCP smoke has been recorded.",),
            ),
            "model_attribution": _capability_record(
                "verified_partial",
                "Allowlisted modelConfig.modelName from each composer row; missing/default values remain unknown.",
                activation="opt_in_project",
                verification=_CURSOR_LIVE,
                limitations=(
                    "No model is inferred from prompt, message, title, or subagent content.",
                    "One real Cursor 3.9.16 store was exercised; missing/default models remain unattributed.",
                ),
            ),
            "cache_read": _unavailable_capability("Hook payload normalization does not report token usage."),
            "cache_write": _unavailable_capability("Hook payload normalization does not report token usage."),
            "automatic_install": _unavailable_capability("agentacct does not install Cursor hooks or client config."),
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
            "verified_stability": _stability_record("not_verified"),
            "capabilities": {
                name: _unavailable_capability("Roadmap entry only; no implementation or verification exists.")
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
    """Validate the manifest's integrity, rejecting any overclaim.

    Enforces the honesty contract: every claim must be backed by evidence
    proportional to its strength.  Called automatically by
    :func:`agent_capability_manifest` before it returns, and directly by tests
    that mutate the manifest to exercise rejection paths.

    Raises :class:`ValueError` (with a message naming the client, and the
    capability lane when relevant) if any of these hold:

      - **Shape**: wrong schema version, ``clients`` not a list, a row or
        capability cell that is not an object or does not use the canonical
        field set.
      - **Client id**: missing, empty, or duplicated.
      - **Client metadata**: a value outside the controlled vocabulary for
        roadmap phase, zero-usage observation, namespace hardening, or
        stability level.
      - **Evidence vs state**: a ``verified`` or ``verified_partial`` state
        without real (``real_fixture`` / ``live_smoke``), dated evidence; or a
        ``synthetic_fixture`` used to back a verified state.
      - **Unavailable overclaim**: an ``unavailable`` lane that claims
        activation, verification, or a usage/cost basis.
      - **Per-lane basis**: a mechanical or MCP lane claiming a usage/cost
        basis (they carry no tokens); a usage or cache lane not marked
        ``client_reported``; an ``automatic_install`` lane labelled with a
        manual activation mode.

    Args:
      manifest: the manifest dict to check.

    Raises:
      ValueError: if any rule above is violated.
    """

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

        # Per-capability checks: each lane must be honest about its evidence and basis.
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
    """Check that dated evidence has a real ISO date and non-empty relative refs.

    Used by :func:`validate_agent_capability_manifest` for any verified-state
    claim or stability record.  ``verified_at`` must parse as an ISO calendar
    date (e.g. ``"2026-07-17"``), and ``evidence_refs`` must be a non-empty
    list of relative paths -- no leading ``/`` or ``..`` traversal, so the
    references stay inside the repo's docs.

    Args:
      evidence: the dict carrying ``verified_at`` and ``evidence_refs``.
      context: a label (e.g. ``"claude-code.usage_import"``) prefixed onto the
        error message so the caller knows which claim failed.

    Raises:
      ValueError: if the date is missing/invalid or the refs are absent/absolute.
    """
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
    """Return the canonical, validated capability manifest (a defensive deep copy).

    The manifest is rebuilt from the ``_CLIENTS`` table on every call and
    validated before returning, so callers can never see an internally
    inconsistent matrix.

    Returns:
      A dict containing:

      - ``schema_version`` -- the manifest format version (frozen for compat).
      - ``last_reviewed_at`` -- the date the evidence was last reviewed.
      - ``support_policy`` -- a short statement of the per-lane independence rule.
      - ``runtime_truth`` -- pointers to the live endpoints that complement this
        static manifest (source detection, ingestion health, evidence).
      - ``clients`` -- one entry per supported agent, each with eight capability
        cells plus client-level metadata.

      The returned dict is a deep copy; mutating it does not affect future calls.

    Callers: ``cli.py`` (the ``agentacct capabilities agents`` command),
    ``api.py`` (the ``/capabilities/agents`` endpoint), and ``evidence_html.py``
    (the matrix renderer).
    """

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
