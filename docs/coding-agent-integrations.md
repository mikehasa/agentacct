# Coding agent integrations

agentacct is meant to fit inside normal coding-agent workflows. Most users run substantial work through Claude Code, Codex, Hermes, OpenCode, OpenClaw, or another MCP-capable agent, not by typing every command directly into a shell.

This page separates the integration promises so the product does not imply more control or precision than it has.

Client support is capability-based, not a binary badge:

| Client | MCP semantics | Local usage path | Mechanical activity | Current product result |
| --- | --- | --- | --- | --- |
| Claude Code | Global or project config can be written | JSONL importer | Installed context/directive bridge; generic Evidence v2 capture remains separate/manual | Usage + MCP sections form semantic Tasks; manual generic capture can form an observed activity Task |
| Codex | Global or project config can be written | SQLite + rollout importer | Global onboard installs an observe-only v1 PreToolUse/SessionEnd hook (trust required); generic Evidence v2 manifest remains manual | Usage + client-log-evidenced MCP sections form semantic Tasks; hook/import activity can enrich Actions |
| Hermes | Global onboard writes profile config; project setup previews it | `state.db` importer | Global onboard installs observe-only v1 shell hooks (consent + gateway restart required); no generic Evidence v2 adapter | Usage + MCP work + hook-observed activity/checks after consent |
| OpenCode | Global onboard writes user config; project setup previews it | Native SQLite `session` rollup importer (JSON export fallback) | Global onboard installs an observe-only v1 plugin; no generic Evidence v2 adapter | Usage + MCP work + plugin/import activity and checks |
| OpenClaw | Manual profile command preview | JSONL importer | Typed plugin hooks and `sessions.json` routing are not integrated yet | Usage plus MCP work when separately configured |
| Cursor | Portable MCP definition only | Primary `state.vscdb` composer observations only; no token importer | Metadata payload normalization exists, but onboarding does not install it | A metadata-only composer Task after explicit refresh/manual capture; usage, cache, and cost unavailable |
| Other MCP clients | Portable stdio definition | None unless a client-specific importer exists | None | MCP work context only |

This prose overview is not the support source of truth. agentacct now exposes a machine-readable, per-capability manifest that keeps runtime detection, ingestion health, and implementation evidence separate:

```bash
agentacct capabilities agents
agentacct capabilities agents --json
curl http://127.0.0.1:8765/capabilities/agents
```

The same matrix is available through the CLI and `/capabilities/agents`. It has no whole-client `supported` boolean: session discovery, usage import, mechanical capture, MCP semantics, model attribution, cache coverage, and installation are each evaluated independently. `experimental` paths have implementation or synthetic-fixture evidence only; `verified_partial` means only the written scope was exercised with real data or a live client. Current files on one machine remain `/usage/sources` truth, and current scan health remains `/ingestion/health` truth.

For a compact matrix of what each path can prove about usage, cost, and budget enforcement, see [Usage and cost truth table](usage-truth-table.md).

## Integration tiers

For reusable per-agent instructions, see [agentacct workflow instructions](agentacct-workflow-instructions.md). A Hermes-compatible skill template is available at [`integrations/hermes/agentacct-workflow/SKILL.md`](../integrations/hermes/agentacct-workflow/SKILL.md).

### Tier 1: MCP tools

MCP setup gives a coding agent access to these nine local agentacct tools:

- `agentacct_list_runs`
- `agentacct_get_report`
- `agentacct_record_machine_check`
- `agentacct_record_event`
- `agentacct_attach_client_context`
- `agentacct_record_section`
- `agentacct_record_agent_usage_debug`
- `agentacct_list_events`
- `agentacct_get_event_summary`
- `agentacct_work_status`

This lets the agent record checkpoints, notes, machine-check evidence, semantic work sections, local client join keys such as session id, parent session id, turn id, request id, and message id, plus debug-only snapshots of usage that the agent can see about itself. It does not automatically parse that agent's private session logs, and it does not give agentacct provider-billed cost control.

### Tier 2: local usage import

Local usage import reads summarized token usage from known local client session stores.

Currently implemented local import paths:

- Claude Code local JSONL session files
- Codex local SQLite/JSONL session data
- Hermes local `state.db` session rows
- OpenCode native `opencode.db` SQLite `session` rollups (per-session token/cost totals), with exported/captured `opencode run --format json` event streams as a fallback when no database is present
- OpenClaw JSONL session logs
- Cursor primary `User/globalStorage/state.vscdb` composer identities, timestamps, explicit model metadata, and exact child links (observation-only; no usage/cost rows)

Measured imports are labeled `client_reported`, and pricing-table estimates are labeled `estimated_from_tokens`. Cursor is deliberately different: the same command surface saves only trusted session observations, so missing usage remains unavailable rather than a measured zero. It rejects symlinked source components, active WAL state, schema drift, corrupt JSON/SQLite, replacement races, and invalid parent graphs; it never falls back to `state.vscdb.backup` or ai-tracking stores. Other agents need client-specific importers because every client can store sessions in a different format.

To select a non-default Cursor application-support root explicitly:

```bash
agentacct usage import-local --client cursor --cursor-home "/path/to/Cursor" --dry-run --json
```

When both tiers are available, agentacct should use local usage import for token/cost truth and MCP section/context/debug events for human-readable attribution and comparison evidence. The context bridge connects these layers with deterministic local identifiers such as client session, transcript, parent/root, and run IDs, then labels the join confidence instead of silently merging unlike data sources.

### Tier 3: mechanical capture

There are two separate mechanical paths:

- **Installed client-specific v1 bridges.** Global onboarding installs Codex PreToolUse/SessionEnd hooks, Hermes shell hooks, and an OpenCode plugin. They spool allowlisted activity/lifecycle facts and recognized check exit codes into the existing product/import paths; they never grant control or report token/cost truth. Codex needs one-time hook trust, Hermes needs one-time hook consent plus a gateway restart, and every client needs a new session. Codex/OpenCode transcript import may supersede overlapping hook activity rather than double-count it.
- **Generic Evidence v2 manifests.** Metadata-only normalizers and render-only manifests exist for Claude Code, Codex, and Cursor. `agentacct capture manifest` does not edit active host settings, and onboarding does not enable these manifests. If manually wired, accepted observations can appear as bounded activity-only Tasks with observed models/checks. The installed Claude Code context/directive bridge is also separate: it is the legacy MCP-join path, not proof that generic Evidence v2 capture is enabled.

## Setup helpers

All examples keep agentacct state project-local under `.agent-sentinel/state`.

For a first project setup, install agentacct with `pipx install agentacct` and follow [`INSTALL.md`](../INSTALL.md), or paste the setup prompt into the coding agent already working in the target repo and let it follow the same runbook.

Print the prompt for any recognized setup target (the short line is identical for all of them; `--full` prints the self-contained version):

```bash
agentacct setup prompt --agent claude-code
agentacct setup prompt --agent codex --full
```

Under the hood, prompt-first setup still uses `init`:

```bash
agentacct init --agent codex
agentacct init --agent codex --write-mcp
agentacct doctor
agentacct mcp doctor
```

Claude Code worktrees are handled in code: `init` and `setup mcp` remap a temporary `.claude/worktrees/<name>` directory to the owning repository, so committed MCP config never embeds a vanishing worktree store path (a worktree with its own pre-existing store keeps it).

`init` creates observe-only project policy/state, updates `.gitignore`, adds short workflow instructions to the appropriate agent instruction file, and previews MCP setup. The `--write-mcp` flag is explicit opt-in and only writes implemented project-local MCP config for Claude Code and Codex. For Hermes, OpenCode, OpenClaw, and generic MCP agents, agentacct prints the command/config because those clients manage MCP servers outside this repo or through client-specific profiles.

That paragraph describes `--scope project`. Default `agentacct onboard` uses global scope: it directly writes supported user-level configuration for Claude Code, Codex, Hermes, and OpenCode and installs their client-specific bridge/hook/plugin where implemented. Those installed v1 surfaces are not the generic Evidence v2 manifests described above.

If `agentacct` is not on the agent's normal PATH, pass `--mcp-command <absolute-path-to-agentacct>` to `init` or `setup mcp` so future agent sessions can start the same MCP server. Use a durable executable path, not a throwaway temp virtualenv.

### Claude Code

```bash
agentacct init --agent claude-code
agentacct init --agent claude-code --write-mcp
agentacct setup mcp --agent claude-code
agentacct setup mcp --agent claude-code --write
agentacct mcp doctor
```

The `--write` path updates project `.mcp.json` with a portable store path.

MCP registration alone does not make Claude Code sessions record work context. The proven recipe (see the Claude Code section of `INSTALL.md`, the canonical runbook) adds `agentacct hooks claude-code install`, then merges both the "hooks" and "env" blocks from the example settings into `.claude/settings.local.json`: the SessionStart hook entry delivers the record-your-work directive, `ENABLE_TOOL_SEARCH=auto` in the `env` block makes the agentacct tools arrive directly callable (deferred tools plus an un-primed session record nothing), and the hook bridge captures the session/transcript ids for high-confidence joins. Exact attribution still requires ids explicitly authored on the recording call. `agentacct hooks claude-code doctor` checks the legacy bridge, not the separate generic Evidence v2 manifest.

### Codex

```bash
agentacct init --agent codex
agentacct init --agent codex --write-mcp
agentacct setup mcp --agent codex
agentacct setup mcp --agent codex --write
agentacct mcp doctor
```

The `--write` path updates project `.codex/config.toml` with a portable store path. Some Codex versions may not load project-local `.codex/config.toml`; in that case use the previewed `codex mcp add ...` command or per-command config flags.

`agentacct onboard --scope global --agent codex` instead writes user-scope MCP config and standing instructions and installs `~/.codex/hooks/agentacct_codex_hook.py` plus its PreToolUse/SessionEnd rows in `~/.codex/hooks.json`. Start a new session and approve Codex's one-time hook trust before expecting that observe-only v1 bridge to fire. It is separate from the render-only generic Evidence v2 Codex manifest.

Local usage import recognizes both Codex's current paginated
`item_completed` / `McpToolCall` rollout records and the older
`function_call`, `function_call_output`, and `mcp_tool_call_end` carriers.
When Codex writes the same logical MCP call in more than one representation,
agentacct reconciles them by call id so the receipt and Action are counted
once. A clean failed or unknown duplicate does not suppress a valid receipt.
Malformed carriers, identity conflicts, and conflicting successful event ids
invalidate that logical call and report evidence schema drift.

No migration or backfill runs automatically; rows not revisited by an explicit
refresh remain unchanged. Restart a long-running watcher after upgrading so
subsequent scans use the new parser.

### Hermes

```bash
agentacct setup mcp --agent hermes
```

Previewed command:

```bash
hermes mcp add agentacct --command agentacct --args mcp serve --store-dir .agent-sentinel/state
```

For project-scope setup, Hermes stores MCP servers in the active profile, so agentacct previews the command and does not write it. `agentacct onboard --scope global --agent hermes` does write the user profile registration and its client-specific v1 hooks (`pre_tool_call`, `post_tool_call`, `on_session_end`, and the first-turn `pre_llm_call` recording nudge). Hermes still requires one-time hook consent and a running-gateway restart; uneditable hooks YAML is left untouched and reported as tools-only. Hermes has no generic Evidence v2 manifest adapter.

Maintainer-probed on VPS with isolated `HERMES_HOME`: Hermes connected to `agent-sentinel mcp serve` and discovered the complete Sentinel MCP surface available in that dated build. (Observed pre-rename; the current nine-tool surface is listed above.)

### OpenCode

```bash
agentacct setup mcp --agent opencode
```

Previewed command:

```bash
opencode mcp add agentacct -- agentacct mcp serve --store-dir .agent-sentinel/state
```

For project-scope setup, agentacct previews the OpenCode user-config command and does not write it. `agentacct onboard --scope global --agent opencode` writes the XDG-aware user MCP config, global `AGENTS.md`, and `plugins/agentacct.js`; a new OpenCode session auto-loads that observe-only v1 plugin. OpenCode has no generic Evidence v2 manifest adapter.

Maintainer-probed on VPS with isolated `HOME`: OpenCode 1.17.11 added the server and reported it connected.

### OpenClaw

```bash
agentacct setup mcp --agent openclaw
```

Previewed command:

```bash
openclaw mcp add agentacct --command agentacct --arg mcp --arg serve --arg --store-dir --arg .agent-sentinel/state
```

OpenClaw stores MCP server config in its active OpenClaw profile. agentacct previews the command and does not write OpenClaw profile config.

Maintainer-probed on VPS with an isolated OpenClaw profile: OpenClaw 2026.6.10 saved a stdio MCP server definition for Sentinel.

### Generic MCP-capable agents

```bash
agentacct setup mcp --agent generic
```

Use the previewed stdio server definition in the agent's own project-local MCP config if it has one:

```json
{
  "mcpServers": {
    "agentacct": {
      "command": "agentacct",
      "args": ["mcp", "serve", "--store-dir", ".agent-sentinel/state"]
    }
  }
}
```

## Maintainer real-client smoke results

Sanitized smoke results from 2026-06-28 on the maintainer VPS (observed pre-rename: the registration key and tool prefixes were `agent-sentinel`, preserved exactly as recorded):

| Client | Provider credential path | Result | What was verified |
| --- | --- | --- | --- |
| Hermes | DeepSeek API key | Passed | Isolated `HERMES_HOME`, Sentinel MCP enabled, Hermes called `sentinel_record_event`, Sentinel event summary contained `hermes-real-smoke`. |
| Hermes | OpenAI Codex OAuth | Passed | Isolated `HERMES_HOME` with copied auth, provider `openai-codex`, Sentinel MCP enabled, Hermes called `sentinel_record_event`, Sentinel event summary contained `hermes-openai-codex-real-smoke`. |
| OpenCode | DeepSeek API key | Passed | Isolated `HOME`, `opencode mcp add ...`, model `deepseek/deepseek-chat`, OpenCode called `agent-sentinel_sentinel_record_event`, Sentinel event summary contained `opencode-real-smoke`. |
| OpenClaw | DeepSeek API key via isolated custom provider | Passed | Isolated OpenClaw profile, `openclaw mcp add ...`, custom `deepseek` OpenAI-compatible provider, OpenClaw called `agent-sentinel__sentinel_record_event`, Sentinel event summary contained `openclaw-real-smoke`. |

Blocked or not tested:

- OpenCode with OpenAI/Claude was not run because the server had no OpenCode OpenAI/Anthropic credentials; `opencode auth list` showed only the DeepSeek environment variable.
- OpenClaw with Claude CLI was probed but blocked by OpenClaw isolated-profile gateway secret resolution, not by Sentinel MCP.
- Raw OpenAI and Anthropic API-key tests were not run because no `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` was configured for these clients.

These are MCP/event smokes. They prove the agents can discover/call agentacct MCP tools under the tested auth paths. They do not prove local token import for those clients, exact subscription billing, or hard budget enforcement outside agentacct-owned paths.

## What this does not claim

- MCP setup does not automatically monitor all agent sessions.
- MCP setup does not parse local token usage unless a client-specific importer exists.
- MCP setup does not provide exact subscription billing.
- agentacct can only pause/kill processes it launched and recorded as agentacct-owned.

For normal Claude Code/Codex sessions, use local usage import for token summaries:

```bash
agentacct usage import-local --client all --dry-run --json
agentacct usage import-local --client all --estimate-costs
```

## Explicit per-client import recipes

`--client all` discovers the default local session stores. When a client's data lives somewhere non-default (or was captured/exported by hand), point the importer at it explicitly.

Import a captured OpenCode JSON stream directory:

```bash
opencode run "..." --format json > /tmp/opencode-smoke.jsonl
agentacct usage import-local --client opencode --opencode-home /tmp --json
```

Import Hermes local state explicitly:

```bash
agentacct usage import-local --client hermes --hermes-home ~/.hermes --json
```

Import OpenClaw JSONL logs explicitly:

```bash
agentacct usage import-local --client openclaw --openclaw-home ~/.openclaw --json
```

Run one background-style scan, or keep scanning on an interval (the watcher does not wrap or launch your coding agents; it periodically imports sessions from implemented local usage paths that clients have already written to disk):

```bash
agentacct usage watch --once
agentacct usage watch --interval-seconds 60
agentacct usage watch --interval-seconds 60 --refresh
```

The import/watch contract, stated exactly:

- **Once by default.** `usage import-local` and `usage watch` record each session (per model lane) once, at first observation, and never update it afterward. A session that keeps growing stays at its first-seen totals on this path — re-running the importer writes nothing for it.
- **Explicit `--refresh` replaces.** `usage import-local --refresh` and `usage watch --refresh` replace re-observed rows whose totals changed; use it on a watch daemon when you want growing sessions' totals to stay current. Opening a TUI/app view or reading a JSON endpoint is not a replacement contract. The former HTML product pages and their `/raw` preview route are retired.
- **One watch daemon per store is the recommended setup.** Concurrent importers (extra daemons or manual imports) are crash-safe — every writer serializes on an advisory file lock and replaces rather than duplicates — but they are redundant.
- **Upgrade note:** a long-running `usage watch` daemon started on a build that predates `--refresh` keeps the once-only default behavior until restarted — harmless (totals and evidence links only lag; nothing is double-counted), but restart daemons on upgrade as usual.
- **Upgrade note (localhost guard):** the local API rejects any request whose `Host` header is not loopback (`localhost`/`127.0.0.1`/`[::1]`), to close DNS-rebinding and cross-site POSTs from the user's own browser. Non-browser clients are unaffected — they send no `Origin` — but a client that reaches the server through a NON-loopback hostname (for example a Docker container calling the host via `host.docker.internal`, or a custom hosts-file alias) now gets a `403` that names the fix: add that hostname with `--allow-host <name>` on `serve`/`api serve`.
