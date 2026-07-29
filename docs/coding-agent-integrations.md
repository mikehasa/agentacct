# Coding agent integrations

agentacct is meant to fit inside normal coding-agent workflows. Most users run substantial work through Claude Code, Codex, Hermes, OpenCode, OpenClaw, or another MCP-capable agent, not by typing every command directly into a shell.

This page separates the integration promises so the product does not imply more control or precision than it has.

The current build is private and unpublished. Client support is capability-based, not a binary badge:

| Client | MCP semantics | Local usage path | Mechanical activity | Current product result |
| --- | --- | --- | --- | --- |
| Claude Code | Project config can be written | JSONL importer | Installed context/directive bridge; generic Evidence v2 capture remains separate/manual | Usage + MCP sections form semantic Tasks; manual mechanical capture can form an observed activity Task |
| Codex | Project config can be written | SQLite + rollout importer | No installed native hook; generic payload adapter is manual | Usage + client-log-evidenced MCP sections form semantic Tasks; manual mechanical capture can form an observed activity Task |
| Hermes | Manual profile command preview | `state.db` importer | Not installed yet | Usage plus MCP work when the user registers it |
| OpenCode | Manual user-config command preview | Native SQLite `session` rollup importer (JSON export fallback) | Realtime plugin not installed; native `opencode.db` session totals are imported (per-message granularity pending) | Usage plus MCP work when separately configured |
| OpenClaw | Manual profile command preview | JSONL importer | Typed plugin hooks and `sessions.json` routing are not integrated yet | Usage plus MCP work when separately configured |
| Cursor | Portable MCP definition only | Primary `state.vscdb` composer observations only; no token importer | Metadata payload normalization exists, but onboarding does not install it | A metadata-only composer Task after explicit refresh/manual capture; usage, cache, and cost unavailable |
| Other MCP clients | Portable stdio definition | None unless a client-specific importer exists | None | MCP work context only |

This prose overview is not the support source of truth. agentacct now exposes a machine-readable, per-capability manifest that keeps runtime detection, ingestion health, and implementation evidence separate:

```bash
agentacct capabilities agents
agentacct capabilities agents --json
curl http://127.0.0.1:8765/capabilities/agents
```

The same matrix appears under **Advanced → Agent capability coverage**. It has no whole-client `supported` boolean: session discovery, usage import, mechanical capture, MCP semantics, model attribution, cache coverage, and installation are each evaluated independently. `experimental` paths have implementation or synthetic-fixture evidence only; `verified_partial` means only the written scope was exercised with real data or a live client. Current files on one machine remain `/usage/sources` truth, and current scan health remains `/ingestion/health` truth.

For a compact matrix of what each path can prove about usage, cost, and budget enforcement, see [Usage and cost truth table](usage-truth-table.md).

## Integration tiers

For reusable per-agent instructions, see [agentacct workflow instructions](agentacct-workflow-instructions.md). A Hermes-compatible skill template is available at [`integrations/hermes/agentacct-workflow/SKILL.md`](../integrations/hermes/agentacct-workflow/SKILL.md).

### Tier 1: MCP tools

MCP setup gives a coding agent access to local agentacct tools such as:

- `sentinel_record_event`
- `sentinel_attach_client_context`
- `sentinel_record_section`
- `sentinel_record_agent_usage_debug`
- `sentinel_list_events`
- `sentinel_get_event_summary`
- `sentinel_list_runs`
- `sentinel_get_report`

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

agentacct has metadata-only normalizers for selected Claude Code, Codex, and Cursor hook payloads. They remain adapter primitives, not automatic product integrations: onboarding does not install the generic manifests. When manually activated, successful capture writes Evidence v2 and a bounded session observation can appear as an activity-only homepage Task, including observed models and recognized checks. It never fabricates named work, token usage, or cost. The installed Claude Code context/directive bridge is a separate legacy path used for MCP joins.

### Tier 4: provider/API proxy

Provider/API proxy paths can enforce hard budget checks only when traffic flows through agentacct or when a provider returns reliable usage/cost data. This is opt-in and separate from MCP setup.

## Setup helpers

All examples keep agentacct state project-local under `.agent-sentinel/state`.

For a first project setup, the owner must provide an authorized local source checkout. Paste the private-build prompt into the coding agent already working in the target repo, and let it follow that checkout's local `INSTALL.md`. There is intentionally no public fetch URL yet.

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

### Hermes

```bash
agentacct setup mcp --agent hermes
```

Previewed command:

```bash
hermes mcp add agentacct --command agentacct --args mcp serve --store-dir .agent-sentinel/state
```

Hermes stores MCP servers in the active Hermes profile, not in the target repo. agentacct previews the command and does not write Hermes profile config.

Maintainer-probed on VPS with isolated `HERMES_HOME`: Hermes connected to `agent-sentinel mcp serve` and discovered all 8 Sentinel MCP tools. (Observed pre-rename; the tool count reflects that probe's date.)

### OpenCode

```bash
agentacct setup mcp --agent opencode
```

Previewed command:

```bash
opencode mcp add agentacct -- agentacct mcp serve --store-dir .agent-sentinel/state
```

OpenCode stores MCP server config in its own user config. agentacct previews the command and does not write OpenCode config.

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

Repeatable command shape:

```bash
agentacct smoke mcp-client --client hermes --provider deepseek --i-understand-this-uses-real-api --json
agentacct smoke mcp-client --client opencode --provider deepseek --i-understand-this-uses-real-api --json
agentacct smoke mcp-client --client openclaw --provider deepseek --i-understand-this-uses-real-api --json
```

The acknowledgement flag is required because these smokes can spend real provider credits.

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

These are MCP/event smokes. They prove the agents can discover/call agentacct MCP tools under the tested auth paths. They do not prove local token import for those clients, exact subscription billing, or hard budget enforcement outside agentacct-owned/proxy paths.

## What this does not claim

- MCP setup does not automatically monitor all agent sessions.
- MCP setup does not parse local token usage unless a client-specific importer exists.
- MCP setup does not provide exact subscription billing.
- agentacct can only pause/kill processes it launched and recorded as agentacct-owned, or enforce budgets on opt-in proxy paths.

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
- **The dashboard button replaces.** The dashboard's "Refresh & save usage" button (`POST /usage/import-local`) replaces every re-observed row with fresh totals, scanning up to 500 recent sessions per client (the CLI default is 20, max 200). Loading or reloading a dashboard page never writes; since the Phase 3.5 page split, the product pages (`/`, `/tokens`, `/sessions`) render saved rows only, and the read-only live scan runs only on the `/raw` page (the Preview action). Old `/?tab=raw` / `/?tab=product` links keep working via 302 redirects.
- **`--refresh` opts the CLI into the same replace semantics.** With `--refresh`, `usage import-local` and `usage watch` also replace re-observed rows with fresh totals on every scan — use it on a watch daemon when you want growing sessions' totals to stay current without the dashboard.
- **One watch daemon per store is the recommended setup.** Concurrent importers (extra daemons, manual imports, dashboard clicks) are crash-safe — every writer serializes on an advisory file lock and replaces rather than duplicates — but they are redundant.
- **Upgrade note:** a long-running `usage watch` daemon started on a build that predates `--refresh` keeps the once-only default behavior until restarted — harmless (totals and evidence links only lag; nothing is double-counted), but restart daemons on upgrade as usual.
- **Upgrade note (localhost guard):** the local dashboard/API and cost proxy now reject any request whose `Host` header is not loopback (`localhost`/`127.0.0.1`/`[::1]`), to close DNS-rebinding and cross-site POSTs from the user's own browser. Non-browser clients are unaffected — they send no `Origin` — but a client that reaches the server through a NON-loopback hostname (for example a Docker container calling the host's proxy via `host.docker.internal`, or a custom hosts-file alias) now gets a `403` that names the fix: add that hostname with `--allow-host <name>` on `serve`/`api serve`/`cost proxy`.
