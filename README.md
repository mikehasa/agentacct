# agentacct

Local-first Agent Work Intelligence for coding agents: usage truth, recorded work, and honest joins.

agentacct is local-first Agent Work Intelligence for coding agents:

- a usage ledger of client-reported tokens, imported from the coding agent's own local session files;
- an MCP work context: sections and events the agent records while working, joined to usage with honest confidence labels;
- an additive Evidence v2 store that keeps native hooks, OTLP spans, MCP claims, orchestrator snapshots, Git checkpoints, and provider evidence separate by source and authority;
- a Task Intelligence dashboard that turns those records into an evidence-backed decision brief;
- a managed local runtime for continuous usage sync and the dashboard.

Client logs are the usage truth. Recorded sections and events are the work meaning. The product is the join between the two: every attribution carries a confidence label, and a missing attribution always beats a wrong one.

> **Status:** early alpha, local-first. Working today: one-command project onboarding for Claude Code and Codex; live-observed local usage paths for Claude Code, Codex, and the bounded Hermes `state.db` scope; experimental synthetic-fixture import paths for OpenCode exports and OpenClaw JSONL; a single-version-live-observed, observation-only Cursor primary `state.vscdb` path; MCP work context; metadata-only Claude Code/Codex/Cursor payload normalization into Evidence v2 when manually wired; mechanical-only Session/Task projection with observed models and recognized checks; local OTLP ingestion; read-only Paperclip/OpenLIT/Entire connectors; local evidence views; and an opt-in control plane limited to agentacct-owned local processes. Generic mechanical capture is not installed by onboarding, does not provide token/cost truth, and does not invent work meaning. Activation remains an explicit project-local action. Interfaces may change.

## What works today

- Join imported usage to recorded work with labeled confidence (`exact`/`high`/`medium`/`low`); when agentacct cannot prove a link, the dashboard shows a dash instead of a guess.
- Record work sections and machine checks from any MCP-capable agent (`sentinel_record_section`, `sentinel_record_machine_check`) and roll them up into attributed sessions and work items on the dashboard's Sessions page.
- Capture real Claude Code session/transcript ids with the installed hook bridge (at session start and on every tool call) — high-confidence join keys; exact attribution still requires ids explicitly authored on the recording call.
- Import client-reported token usage through the declared per-agent paths. Claude Code/Codex and bounded Hermes usage have live local evidence; OpenCode export and OpenClaw JSONL paths remain experimental and synthetic-fixture verified. Inspect `agentacct capabilities agents --json` or **Advanced → Agent capability coverage** for exact scope and limitations.
- Connect a project with `agentacct onboard`, then keep continuous local usage sync and the dashboard alive independently of the invoking shell.
- Open a Task decision brief with independent execution, outcome, and control state plus a bounded evidence timeline.
- Browse everything on a local dashboard/API at `127.0.0.1`: detected local sources, usage, events, sections, and attribution rollups.
- Record local events from the CLI, API, or MCP tools.
- Inspect the Work Graph, Evidence Matrix, Discrepancies, and Cost & Outcome Basis without collapsing claims into observations.

## What it does not claim yet

- No hosted dashboard or phone-home telemetry. agentacct can receive telemetry locally when you explicitly point an OTLP exporter at it.
- No automatic cloud account sync.
- No exact Claude Code/Codex subscription invoice access.
- No silent monitoring of unrelated processes started outside agentacct/integrations.
- No universal process control: hard stops apply only to agentacct-owned runs or the opt-in proxy path (see Optional enforcement extras below).

## Safety defaults

- Local-first state; no phone-home telemetry.
- Provider API keys are not stored by agentacct and must come from environment variables.
- Observe-only setup does not require provider API keys.
- Dashboard/API bind to localhost by default.
- Provider forwarding is disabled by default and requires an explicit budget cap; agentacct only pauses/kills processes it launched and recorded as agentacct-owned.

## What “telemetry” means here

Telemetry is machine-emitted evidence about a running agent: for example a session start, a tool span, a trace/span id, a model label, a duration, or a client-reported token field. OpenTelemetry/OTLP is one standard wire format for those records; OpenLIT instruments runtimes and exports that format.

It is not the same thing as MCP. MCP is an agent-to-tool protocol and is especially useful for semantic claims such as “I started this task”, “this section is complete”, or “this blocker occurred”. A native hook is a host callback that fires mechanically. A local client log is the client's own persisted usage/session record. agentacct accepts all four, keeps their provenance, and decides authority per evidence dimension. OTLP arriving locally is not automatically provider billing truth, and a missing MCP event does not mean no work happened.

## Install

Requires Python >= 3.11 on macOS or Linux; Windows is supported only via WSL.

The fastest path is to paste this into the coding agent working in your target repo:

```text
Install and set up agentacct in this repo — a local-first dashboard that reads my
coding-agent logs read-only and shows honest token usage and cost.

Run `pipx install agentacct`
(or `pipx install git+https://github.com/mikehasa/agentacct`),
then `agentacct onboard` from this repo root, then tell me the dashboard URL.

Observe-only: never store, request, or echo any API key; all state stays local
under `.agent-sentinel/`. Don't modify my global client config without showing
the exact command first.
```

The agent then follows [INSTALL.md](INSTALL.md), the canonical runbook with the public install path, an advanced manual fallback, and an honest per-client capability matrix. `agentacct setup prompt --agent <client>` prints this same prompt as a single line; add `--full` for a self-contained prompt.

Preferred CLI path, no agent involved. Install the CLI, then run onboarding from the target repo root:

```bash
pipx install agentacct
# or, straight from the public repository:
#   pipx install git+https://github.com/mikehasa/agentacct
agentacct onboard
agentacct status
```

`onboard` detects a known local client source, initializes the project-local store and recording configuration, performs one real local usage sync, and starts continuous sync plus the dashboard. Detection means data is present on this machine; it is not a whole-client support badge. It does not request provider keys, connect to a billing account, or modify global client settings. Then open a **NEW** agent session in this project: MCP servers and hooks bind when the client session starts, so the session that ran onboarding cannot become the first recorded Task.

The managed runtime has an idempotent lifecycle:

```bash
agentacct start   # start continuous sync and the dashboard
agentacct status  # inspect dashboard, sync, and ingestion readiness
agentacct stop    # stop only processes agentacct can prove it owns
agentacct repair  # clear dead/corrupt owned runtime state safely
```

The default dashboard is `http://127.0.0.1:8765`. `status`, `stop`, and `repair` use the same project-local store; they refuse to treat an unknown process as agentacct-owned.

Advanced manual setup fallback. Use this when client auto-detection is unavailable or you need to configure each integration separately:

```bash
# Codex (see INSTALL.md for Hermes/OpenCode/OpenClaw):
agentacct init --agent codex --write-mcp
agentacct doctor

# Claude Code — the hook bridge is the attribution keystone:
agentacct init --agent claude-code --write-mcp
agentacct hooks claude-code install
# then merge BOTH the "hooks" and "env" blocks (including ENABLE_TOOL_SEARCH=auto)
# from .claude/settings.agent-sentinel.example.json into .claude/settings.local.json
agentacct hooks claude-code doctor

# Either client:
agentacct mcp doctor
agentacct serve
```

agentacct state is project-local under `.agent-sentinel/` (gitignored; the store directory keeps the pre-rename name `.agent-sentinel/` for compatibility with existing stores). Observe-only setup does not require provider API keys and does not connect to billing APIs. The new-session requirement applies to both onboarding paths.

Optional zero-key demo — `agentacct demo` alone uses a throwaway temp store; after `init`, keep demo runs in the project store and browse them:

```bash
agentacct init
agentacct demo --store-dir .agent-sentinel/state
agentacct report latest
agentacct serve
```

Open:

```text
http://127.0.0.1:8765
```

The demo uses only local commands. It does not require provider API keys and does not make paid API calls.

## Task Intelligence

The top-level product object is a Task, not an individual event or sub-run. A recognized root client session creates the default observed Task boundary; explicitly linked continuation sessions can remain part of the same Task. Public routes use a stable opaque Task id rather than exposing a raw client session id:

```text
http://127.0.0.1:8765/tasks/task_<opaque-id>
http://127.0.0.1:8765/api/tasks/task_<opaque-id>
```

The Task page leads with a decision brief: what was attempted, the current outcome, the strongest proof, usage/cost basis, unresolved findings, and a next action only when an owner was actually recorded. The evidence timeline is available underneath for inspection.

Task status is deliberately split into three independent axes:

- **Execution:** whether an attempt is queued, running, finished, cancelled, or lost.
- **Outcome:** whether the result is unknown, agent-reported, verified, blocked, or has an open finding.
- **Control:** whether agentacct is ready, awaiting approval, holding on policy, or encountered a control failure.

This keeps a failed target-product check from being mislabeled as a agentacct failure or automatically turned into a user action. See [Task Intelligence and the local control plane](docs/task-control-plane.md) for the contract.

## Local Control

The **Control** page is a separate, opt-in surface for processes agentacct itself launches. It does not adopt or signal an existing Codex, Claude Code, Paperclip, OpenLIT, Entire, or other orchestrator process. Normal agent sessions remain observed on **Work**.

Register one explicit workspace and one fixed argv adapter, then create a reviewable Task Contract:

```bash
agentacct control register-workspace \
  --store-dir .agent-sentinel/state --root . --workspace-id project

agentacct control register-agent \
  --store-dir .agent-sentinel/state \
  --agent-id local-agent --display-name "Local agent" \
  --argv-json '["/absolute/command","arg"]'

agentacct control plan \
  --store-dir .agent-sentinel/state \
  --objective "Run the bounded local task" \
  --workspace-id project --agent-id local-agent \
  --permission-envelope-json '{"mutation_mode":"read_only"}' \
  --success-check "the owned process exits successfully"
```

Open `http://127.0.0.1:8765/control` to review pending attempts, independent execution/outcome/control state, approvals, budgets, and schedules. Nothing launches when a contract is created. The dashboard keeps a persistent local supervisor while it is running; the CLI `control launch` command deliberately supervises in the foreground instead of pretending a one-shot shell command is a daemon.

Every mutation uses an idempotency key and expected revision. Workspace-write attempts created in the web UI follow `hold → request → approve → consume once → ready`; the supervisor refuses `awaiting_approval`, `policy_hold`, and `control_failure` attempts. Each attempt also freezes the registered agent revision, so changing that agent's argv after approval cannot change what the approval authorizes: create a new attempt for the new revision. Product JSON omits registered paths, argv, PIDs/process groups, executable/cwd fingerprints, manifest ids, and ownership nonces.

## Migration from the pre-rename install

agentacct was formerly Agent Sentinel. Existing installs keep working without re-setup:

- Environment variables: every `AGENT_CHRONICLE_*` variable accepts its pre-rename `AGENT_SENTINEL_*` alias — the old names are accepted indefinitely, the new name wins when both are set, and conflicting store-dir values refuse rather than silently split the ledger. There is no runtime deprecation nag; the old names simply keep working.
- Binaries: the published package ships only agentacct-branded console scripts — `agentacct` plus the `agentacct-claude` / `agentacct-codex` wrappers. Pre-rename MCP registrations, hook wrappers, and launchd jobs that resolve the old `agent-chronicle` / `agent-sentinel` binary names are still recognized at runtime, but the old console scripts are no longer shipped (they collide with unrelated PyPI packages of those names).
- Stored data and the `.agent-sentinel/` store directory keep their pre-rename spellings forever; they are data format, not branding.
- Caveat — foreign PyPI package collision: an unrelated project named `agent-sentinel` (0.5.0) exists on PyPI and also installs a `bin/agent-sentinel` script. Installing both packages into the same environment makes the later install silently overwrite that script (pip prints no warning), and `pip uninstall agent-sentinel` (the foreign package) deletes the alias out from under agentacct — anything resolving the old binary name dies until `pip install --force-reinstall agentacct`. Avoid installing both in one environment; pipx refuses the second install outright because the app names collide.

## How the join works

agentacct keeps two evidence streams separate and joins them on real client ids instead of guessing:

- **Usage truth** comes from the client's own local session files: imported tokens are labeled `client_reported`, and costs are pricing-table estimates — never provider invoices.
- **Work meaning** comes from the sections and events the agent records over MCP while it works.
- **The join** links the two through session/transcript ids and labels every attribution `exact`, `high`, `medium`, or `low`. Missing attribution beats wrong attribution: when the ids do not prove a link, the dashboard shows the gap instead of inventing one.

Per client, the join keys arrive differently:

- **Claude Code** — the installed hook bridge captures real session/transcript ids at session start and on every tool call. Recorded work context needs the full three-lever settings merge: the SessionStart hook entry delivers the record-your-work directive, `ENABLE_TOOL_SEARCH=auto` in the settings `env` block makes the agentacct tools arrive directly callable (without it they stay deferred and un-primed sessions record nothing), and the hook itself supplies the join ids.
- **Codex** — the client cannot pass its own session id in-session. At usage import, agentacct pairs each agentacct-recorded event with the Codex session log that created it (client-log evidence): high confidence, never `exact`, and a section evidenced by more than one session links to none.
- **Everywhere** — inherited ids are never `exact`; only ids passed explicitly on the recording call earn `exact`.

## What each client can claim

<!-- Consistency contract: the capability matrix below is defined in
     src/agent_chronicle/install_guide.py and embedded here verbatim, exactly as
     in INSTALL.md. tests/test_install_guide.py fails if this list drifts. -->

- Claude Code: automatic high-confidence joins between usage and recorded work via the installed hook bridge (SessionStart and PreToolUse capture real session/transcript ids). Exact attribution still requires ids authored explicitly on the recording call; hook-derived ids are not bound to that MCP request. Recorded work context needs all three levers: the merged hooks settings entry including SessionStart (delivery — the SessionStart hook is the only path proven to make un-primed sessions record work, and it adds session-start/resume id capture; PreToolUse still captures the session/transcript ids on every tool call), `ENABLE_TOOL_SEARCH=auto` in the settings `env` block (discoverability — without it the agentacct tools stay deferred and un-primed sessions record nothing), and the hook bridge itself (join keys — without it recorded sections fall back to project-level context, never session-linked).
- Codex: session-linked work context via client-log evidence (high) — at usage import, agentacct pairs each agentacct-recorded event with the Codex session log that created it (creation responses only, never read-tool echoes). Sections still never earn `exact` (the link is evidenced from the log at import time, not client-authored in-session), a section evidenced by more than one session links to none, and sessions whose rollouts have not been imported fall back to project-level context only.
- Hermes: local `state.db` usage import plus a manual MCP registration preview; agentacct does not yet install Hermes mechanical hooks.
- OpenCode: captured/exported JSON usage import plus a manual MCP registration preview; agentacct does not yet parse the current official SQLite store or install a realtime plugin.
- OpenClaw: local JSONL usage import plus a manual MCP registration preview; agentacct does not yet join `sessions.json` routing metadata or install typed plugin hooks.
- Cursor: the primary `User/globalStorage/state.vscdb` can produce observation-only composer sessions through an explicit local import/refresh. It never emits usage or cost, never scans backups or ai-tracking stores, and onboarding does not install or activate it. Metadata-only hook payload normalization remains a separate manual primitive.
- Generic MCP clients: recorded work context only unless a separate trusted usage importer exists; join confidence depends on ids the client actually exposes.
- Mechanical Claude Code/Codex/Cursor capture, when manually wired, writes Evidence v2 and projects bounded session activity into the homepage as an observed Task with models/checks when present. It is not activated by onboarding, does not report token/cost truth, and does not invent named work steps; MCP remains the richer semantic source.
- All clients: imported tokens are client_reported (read from the client's own local session files); costs are estimates from a local pricing table — never provider invoices.
- Always: local-first, observe-only, no telemetry, no provider API keys stored or requested.

## Daily workflow: import usage and browse

```bash
# 1. See what agentacct can read on this machine.
agentacct usage discover-sources

# 2. Start the local dashboard. Product pages render saved rows; the /raw page
#    runs the read-only local scan.
agentacct serve

# 3. Save current local usage summaries into agentacct's ledger.
agentacct usage import-local --client all

# 4. Optionally keep importing new sessions in the background
#    (add --refresh to also update growing sessions' totals).
agentacct usage watch --interval-seconds 60

# 5. In another terminal, inspect per-source receipts and watcher health.
agentacct usage health
```

The primary dashboard has three destinations. **Work** (`/`) is one compact, deduplicated feed of named work and otherwise-unlabelled local activity. Explicit blockers are user-action items; unresolved failed checks remain non-actionable open findings until the agent resolves them, rather than being assigned to the user. Completed work is labeled `Verified` when a current passing machine check is linked and `Agent reported` otherwise. Attribution gaps, missing join keys, source coverage, and evidence conflicts remain available in the full `/sessions` explorer and **Advanced** (`/advanced`) instead of becoming user-facing tasks. **Usage** (`/tokens`) owns token, cache, cost, and confidence analysis. The stable `/raw`, `/work-graph`, `/evidence-matrix`, `/discrepancies`, and `/cost-outcome-basis` routes remain available under Advanced. Old `/?tab=raw` / `/?tab=product` links keep working via 302 redirects.

Evidence **Source coverage** is historical evidence, not a connection monitor: “Evidence received” means agentacct saved records from that source, and the connection remains `not_verified` without an independent live check. **Activity sync** is separate operational state. It comes from durable per-source scan receipts plus the watcher's lease/heartbeat, so the Work page can distinguish a successful manual refresh, a live watcher, and a degraded or stale synchronizer. Full source, attribution, and evidence explanations stay inspectable in Sessions and Advanced.

Preview any import with `--dry-run`, and add `--estimate-costs` to attach pricing-table cost estimates. A dry run returns source diagnostics but writes neither usage nor health state. A real import or dashboard refresh atomically records per-source attempt/success timestamps, parsed/skipped/error counts, watermark, and scan-limit diagnostics under the selected store. Inspect them with `agentacct usage health --json` or `GET /ingestion/health`; `GET /health` keeps service liveness (`ok`) separate from `ingestion_status`.

The watcher does not wrap or launch your coding agents; it periodically imports sessions from implemented local usage paths that clients have already written to disk. By default the CLI and watcher record each session once, at first observation, and never update it — a session that keeps growing stays at its first-seen totals until the dashboard's **Refresh** action replaces re-observed rows with fresh totals, or you pass `--refresh` to `usage import-local` / `usage watch`. One live watcher owns a store through a lease; a second is rejected, a lost lease stops the old process, and a stale heartbeat is reported with a restart action. Long-running Python processes do not hot-reload upgraded agentacct code: after every package/source upgrade, stop the existing watcher through the terminal or process supervisor that launched it, start `usage watch` again, and confirm its `importer_version` and fresh heartbeat with `usage health --json`.

For Codex, `--limit-sessions` counts recent **root conversation groups**, not flat thread rows. agentacct resolves Codex-recorded parent edges before applying the limit, then returns the selected root and all discovered descendants so internal reviews cannot crowd their root chat out of the scan. An internal workflow label such as `codex-auto-review` is not a billable model: it inherits a model only from its exact recorded parent, with provenance; otherwise the model stays unknown and unpriced. agentacct never borrows a scan-wide model from another task.

Local imports do not read provider API keys, call provider APIs, or upload session data. Imported usage is labeled `client_reported`; estimated costs are labeled `estimated_from_tokens`, not provider billing. Cursor's local path is observation-only and never contributes usage or cost. Unknown models remain unpriced until a trusted catalog entry exists — pricing paths keep a store-local LiteLLM pricing snapshot fresh automatically (7-day TTL; the fetch is a plain GET of LiteLLM's public open-source price table, not telemetry — nothing is sent; disable with `AGENT_CHRONICLE_PRICING_AUTO_REFRESH=0`; details in [docs/usage-truth-table.md](docs/usage-truth-table.md)). Explicit per-client import flags (`--opencode-home`, `--hermes-home`, `--openclaw-home`, `--cursor-home`) are documented in [docs/coding-agent-integrations.md](docs/coding-agent-integrations.md).

To see what each integration path can and cannot prove:

```bash
agentacct usage truth-table
```

## Cost and usage confidence

agentacct keeps usage confidence separate from cost confidence so dashboards do not imply more precision than the data supports.

Common labels:

```text
usage_confidence:
  provider_reported       provider returned token usage
  client_reported         local client session store reported token usage
  estimated               agentacct estimated usage before/without provider data
  unknown                 unavailable

cost_confidence:
  provider_billed                         provider returned a finite non-negative cost
  estimated_from_tokens                   agentacct estimated cost from tokens and pricing table
  approximate_subscription_allocation      user-entered subscription allocation
  subscription_unavailable                no fair subscription allocation denominator
  unknown                                 unavailable

cost_basis:
  provider_invoice
  local_client_session
  pricing_table
  user_subscription
  none
```

How to read the labels:

- `provider_billed`: safe basis for hard dollar budget enforcement.
- `estimated_from_tokens` or subscription allocation: useful for warnings, dashboards, and opt-in hard stops.
- `unknown`: use token, runtime, or checkpoint budgets instead of dollar hard stops.

### Subscription allocation

Claude Code/Codex subscriptions usually do not expose exact per-run billing to agentacct. Save a plan with `agentacct cost subscription-set`, then estimate an allocation from local events:

```bash
agentacct cost subscription-estimate --subscription-name claude-code-pro
```

When no fair allocation denominator exists, the result is labeled `subscription_unavailable`. This is an explicit approximation, not as exact provider billing. It is an allocation model, not invoice truth. Provider invoices or exposed provider usage data remain the source of truth.

## Local events and MCP tools

Record a note and summarize events:

```bash
agentacct event note "Reviewed Codex output"
agentacct event summary
agentacct event list
```

Run the local dashboard/API:

```bash
agentacct serve
curl http://127.0.0.1:8765/usage/sources
curl http://127.0.0.1:8765/usage/preview
curl http://127.0.0.1:8765/usage/summary
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/ingestion/health
curl http://127.0.0.1:8765/events/summary
```

`/events/summary?limit=N` keeps its recent-event aggregates bounded, but join-health counters and coverage ratios are computed over every matching event in the store. Machine consumers must inspect `result_scope.partial` and the bridge's `detail_scope.partial`: `links`, `attributions`, and `unlinked_contexts` may be capped even when the canonical full-store ratios are complete. A degraded response includes stable `degraded_reasons` instead of treating one successful join as healthy.

Evidence v2 is additive and enabled by default. It shadows existing v1 writes; it does not replace `events.jsonl` or rename any public `sentinel_*` MCP tool. Inspect or replay it with:

```bash
agentacct evidence status --store-dir .agent-sentinel/state
agentacct evidence replay-v1 --store-dir .agent-sentinel/state
agentacct evidence product --store-dir .agent-sentinel/state --json
```

Mechanical capture is opt-in. These commands disclose capabilities and render a fragment, but do not edit active host configuration:

```bash
agentacct capabilities agents
agentacct capabilities agents --json
agentacct capture capabilities --json
agentacct capture manifest --vendor claude-code
agentacct capture manifest --vendor codex
agentacct capture manifest --vendor cursor
```

`capabilities agents` is the canonical per-agent manifest. It deliberately has no whole-client support badge: local session discovery, usage import, mechanical capture, MCP semantics, model attribution, cache read/write, installation mode, and verification evidence remain independent. The Dashboard renders the same truth under **Advanced → Agent capability coverage**; `/usage/sources` and `/ingestion/health` continue to describe this machine's current runtime state instead.

Read-only external adapters support dry runs before durable import:

```bash
agentacct connector openlit /path/to/otlp.json --dry-run --json
agentacct connector paperclip /path/to/paperclip-export.json --dry-run --json
agentacct connector entire /path/to/git-repository --dry-run --json
```

The localhost API accepts OTLP/HTTP JSON at `POST /v1/traces`. Start evidence inspection at `/advanced`; the stable projections remain available at `/work-graph`, `/evidence-matrix`, `/discrepancies`, and `/cost-outcome-basis`. `GET /evidence/events` uses bounded arrival-order cursor pages (`limit`, then `cursor=next_cursor`). Set `AGENT_CHRONICLE_EVIDENCE_V2=0` for a v1-only rollback; existing v2 evidence remains untouched.

Start the MCP server over stdio:

```bash
agentacct mcp serve
```

Current MCP tools include:

- `sentinel_list_runs`
- `sentinel_get_report`
- `sentinel_record_event`
- `sentinel_attach_client_context`
- `sentinel_record_section`
- `sentinel_record_agent_usage_debug`
- `sentinel_list_events`
- `sentinel_get_event_summary`
- `sentinel_record_machine_check`
- `sentinel_prepare_judge`
- `sentinel_compute_value`

Use local usage import for token/cost truth, and use MCP tools for workflow context. `sentinel_attach_client_context` stores local session/turn/message identifiers, while `sentinel_record_section` stores human-readable task chapters that can later be joined to imported usage. `sentinel_record_agent_usage_debug` stores agent-visible usage snapshots for comparison only; it does not add to agentacct's usage or cost totals.

How agents learn to record: the MCP server returns record-your-work instructions in its initialize result, and for Claude Code the SessionStart hook injects the same directive into every new session. `agentacct setup instructions --agent <client> --user` writes a standing instruction block into user-level `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` (idempotent markers; `--remove` strips it, `--dry-run` previews). For one machine-wide store and dashboard instead of per-repo installs, follow the "Global install" section of [INSTALL.md](INSTALL.md); fold older per-project stores in with `agentacct usage merge-store`.

Verify the local MCP event workflow without paid APIs:

```bash
agentacct mcp workflow-smoke --json
```

The workflow smoke does not call Claude, Codex, or provider APIs. By default it records its test events in a throwaway temporary store (the printed store path vanishes on cleanup/reboot); pass `--store-dir` only if you deliberately want smoke events in a persistent ledger.

## Verification and evidence

These helpers configure project-local onboarding. They do not mean agentacct automatically monitors every Claude Code/Codex/OpenCode/Hermes/OpenClaw session you start elsewhere.

Maintainer smoke tests have verified minimal Claude Code and Codex runs, agentacct has been smoke-tested as an MCP tool inside real interactive Claude Code and Codex sessions, and Hermes/OpenCode/OpenClaw MCP setup commands have been maintainer-probed on the VPS. See [docs/live-agent-smoke.md](docs/live-agent-smoke.md), [docs/live-smoke-results.md](docs/live-smoke-results.md), and [docs/live-mcp-client-smoke-results.md](docs/live-mcp-client-smoke-results.md) for sanitized evidence and current limitations.

Release-gate smoke commands, optional and not part of default CI because they can consume paid tokens:

```bash
agentacct smoke claude-code
agentacct smoke codex
agentacct smoke all --json

# Real MCP-client smoke tests. Disabled by default because they use real API credits.
# Requires DEEPSEEK_API_KEY in the environment.
agentacct smoke mcp-client --client hermes --provider deepseek --i-understand-this-uses-real-api --json
```

## Optional enforcement extras

Enforcement is opt-in and secondary to observation.

agentacct provides explicit wrapper commands for runs you want agentacct to own directly:

```bash
agentacct-claude -p "Reply with exactly: hello"
agentacct-codex exec --sandbox read-only --ephemeral --skip-git-repo-check "Reply with exactly: hello"
```

Wrappers launch the underlying `claude` or `codex` binary through agentacct and write normal local run artifacts, with stdout/stderr capture, timeouts, and pause/kill limited to agentacct-owned runs. They do not automatically monitor Claude Code/Codex sessions started outside these wrapper commands, and they do not provide exact subscription billing by themselves.

An opt-in provider cost proxy (`agentacct cost proxy --enable-forwarding --max-total-usd ...`) can forward OpenRouter/OpenAI/DeepSeek API calls under a hard local budget cap: forwarding is disabled by default, requires an explicit cap, refuses non-local bind hosts by default, and stores provider-reported costs as `actual_provider_cost_usd`. Start with low-limit test keys; details in [Safety boundaries](docs/safety-boundaries.md).

## More documentation

- [Full flow demo](docs/full-demo.md)
- [Architecture](docs/architecture.md)
- [Task Intelligence and local control plane](docs/task-control-plane.md)
- [Multi-source Evidence v2 architecture](docs/multi-source-evidence-architecture.md)
- [Multi-source privacy threat model](docs/multi-source-privacy-threat-model.md)
- [Integration license BOM](docs/integration-license-bom.md)
- [Safety boundaries](docs/safety-boundaries.md)
- [Public alpha checklist](docs/public-alpha-checklist.md)
- [Live agent smoke guide](docs/live-agent-smoke.md)
- [Live smoke results](docs/live-smoke-results.md)
- [Live MCP client smoke results](docs/live-mcp-client-smoke-results.md)
- [Coding agent integrations](docs/coding-agent-integrations.md)
- [agentacct workflow instructions](docs/agent-chronicle-workflow-instructions.md)
- [Usage and cost truth table](docs/usage-truth-table.md)
- [Prompt budget preview](docs/prompt-budget-optimizer.md)

## Development

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution scope, safety principles, and PR expectations.

Run tests from a clone (the pipx install above ships no test tooling):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest tests/ -q --tb=short
```

## Feedback

agentacct is early alpha. Useful feedback:

- Which agent or tool do you use?
- What runaway, cost, or observability issue did you hit?
- Which join/attribution result looked wrong or missing?
- What report would help you trust a run?
- Which integration should be supported next?

Open an issue with a bug report, feature request, or integration request. Please scrub any provider API keys or private paths from logs before sharing them.
