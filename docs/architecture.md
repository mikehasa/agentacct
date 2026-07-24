# Architecture

Agent Chronicle is local-first Agent Work Intelligence for autonomous coding-agent runs: it records real token usage and the work a session actually did, and joins them honestly. Run-safety features (process ownership, budgets, checkpoints) are secondary. The additive multi-source design is specified in [multi-source-evidence-architecture.md](multi-source-evidence-architecture.md); the Task and Chronicle-owned execution contract is specified in [task-control-plane.md](task-control-plane.md).

It is designed around one principle:

```text
Observe and control only the work Agent Chronicle owns.
```

In the early alpha, Chronicle does not scan or attach to arbitrary existing agent processes. It records runs it starts and accepts evidence only from explicitly configured local sources: client-log import, MCP, host hooks, OTLP, read-only connectors, local API, and CI.

## High-level flow

```text
client logs  MCP claims  host hooks  OTLP  Paperclip  Entire Git  provider/CI
     \          |          |          |       |           |           /
      +---------+----------+----------+-------+-----------+----------+
                                  |
                       normalize + redact + preserve source
                                  |
                    append-only Evidence v2 spool
                                  |
                     indexed SQLite projection
                                  |
        Work Graph | Evidence Matrix | Discrepancies | Cost/Outcome Basis

Existing v1 events.jsonl, usage ledger, reports, and run-control paths remain
active beside this additive evidence plane.
```

Observed sessions and planned work project into stable Tasks. A separate
append-only Control Store records Chronicle-owned attempts, approvals, policies,
schedules, registered workspaces, and audit events. Evidence can inform control;
it cannot grant itself dispatch authority.

## Main components

### CLI

The CLI is the primary development and debugging interface.

It can:

- run a command under Chronicle ownership
- onboard a known local usage source and manage the dashboard/sync runtime
- inspect a Task decision brief and its evidence timeline
- create and govern Chronicle-owned local Task attempts
- initialize and validate project-local policy
- show run reports
- record outcome evidence
- prepare isolated judge packages
- run an opt-in OpenRouter judge
- compute advisory value scores
- serve the local API
- serve MCP tools over stdio
- render metadata-only Claude Code, Codex, and Cursor hook fragments without activating them
- import OpenLIT/OTLP, Paperclip exports, and Entire Git checkpoints through read-only adapters
- inspect/replay Evidence v2 through the Work-first dashboard, Advanced hub,
  and four stable evidence projections

The default Work projection is deliberately narrower than the ledger. It
deduplicates named work and session-only activity, scopes explicit project
labels to the current project store, and turns only blockers or failed checks
into user actions. Reconciliation gaps and source coverage remain available in
Sessions and Advanced; they are system diagnostics, not assumed user work.

### Local run store

The run store is a local directory of run artifacts. It contains files such as:

- metadata
- stdout/stderr logs
- markdown reports
- outcome evidence
- judge packages
- cost events

By default this is local state, not a cloud service.

### Task and control stores

Observed root sessions create the default Task boundary; explicit continuation
links and planned Tasks preserve a stable opaque public identity. The Task
projection composes saved session, work, usage, check, artifact, and control
records without rewriting them or inventing missing joins.

The Control Store is append-only operational authority, separate from Evidence
v2. It records versioned Task contracts, agent specs, registered workspaces,
attempts, approvals, budget policies, schedules, and immutable control actions.
A single leased supervisor dispatches only Chronicle-owned processes and
reconciles them after restart using a complete process fingerprint and launch
nonce.

### Evidence v2 store

Evidence v2 has a separate fsynced `evidence-v2/spool.jsonl` and rebuildable
`projection.sqlite3`. Immutable envelopes distinguish `observed` from
`claimed`, keep source identity and measurement basis, preserve conflicts, and
deduplicate replays as receipts. Source authority is evaluated per dimension;
no adapter can promote its own claim into provider billing or objective outcome
truth.

### Reports

Reports summarize what happened during a run.

The JSON report is meant for machines:

```bash
agent-chronicle report latest --json
```

The markdown report is meant for humans:

```bash
agent-chronicle report latest
```

### Outcome evidence

Outcome evidence records whether the run appears to have improved anything verifiable.

Examples:

- tests failed before and passed after
- build failed before and passed after
- lint passed before and still passed after
- new failures were introduced

This is deliberately separate from subjective value judgment.

### Judge package and judge scoring

Chronicle can prepare an isolated judge package without spending money.

```bash
agent-chronicle judge prepare latest
```

A paid LLM judge call is opt-in and budget-gated.

```bash
agent-chronicle judge run latest --max-total-usd 0.01
```

The judge output is advisory. It is one signal, not ground truth.

### Advisory value scoring

Value scoring combines:

- deliverable score
- cost efficiency relative to the user's budget
- machine-check evidence
- risk penalties for introduced failures

The score is meant to answer:

```text
Was this run worth it relative to what it cost?
```

It is transparent and human-overridable.

### Cost proxy and ledger

The cost proxy records local estimates, exact token usage when a provider returns it, and provider-reported dollar costs when available.

Real forwarding is disabled by default. In this alpha, OpenRouter, OpenAI, and DeepSeek forwarding require explicit provider allowlists, provider keys, and a local `--max-total-usd` budget cap. Anthropic and Gemini have validation coverage but are not forwarding paths yet.

### Local API

The local API is for sidecar dashboards and local integrations.

It binds to localhost by default:

```bash
agent-chronicle api serve --host 127.0.0.1 --port 8789
```

It exposes report/outcome/value primitives, Evidence v2 inspection, bounded
local capture, read-only connector ingestion, and an OTLP/HTTP JSON receiver.
It does not call paid judge APIs.

The API validates basic local inputs such as run IDs, list limits, and positive value-score budgets. Invalid client inputs return 422 responses.

### MCP server

The MCP server is for agent integrations.

```bash
agent-chronicle mcp serve
```

Current MCP tools are safe/local:

- `sentinel_list_runs`
- `sentinel_get_report`
- `sentinel_record_event`
- `sentinel_attach_client_context`
- `sentinel_record_section`
- `sentinel_record_agent_usage_debug`
- `sentinel_list_events`
- `sentinel_record_machine_check`
- `sentinel_prepare_judge`
- `sentinel_compute_value`

The event tools continue to write/read `events.jsonl` and shadow normalized
claims into Evidence v2. Public `sentinel_*` names do not change. Local usage
import remains the token/cost source of truth; MCP remains the highest-value
source for semantic context and join keys such as client session, parent
session, turn id, request id, and message id. It is not treated as provider
billing or as a mechanical process observation.

### Hooks

Hooks are an optional mechanical adapter layer. Claude Code, Codex, and Cursor
adapters emit metadata-only lifecycle/tool/machine observations into Evidence
v2. `capture manifest` is a pure renderer: it does not modify project or global
settings. The older Claude Code context-bridge installer still writes only
project-local example files and does not activate global settings. In the
current build, accepted Evidence v2 hook observations also feed a bounded,
read-only session-observation projection into the v1-derived Work ledger, so a
mechanical-only session can appear as an activity Task. The projection carries
observed models and recognized checks but never fabricates named work, usage, or
cost; Advanced remains the raw evidence inspector.

## Intended product layers

```text
Preflight:
  onboard, detect sources, validate Task Contract and workspace policy

Runtime:
  owned execution, approvals, budget limits, schedules, checkpoints

Post-run:
  Task Intelligence, reports, outcome evidence, governed retry/cancel

Integration:
  local API, MCP, optional hooks
```

## Non-goals in the early alpha

Agent Chronicle is not currently:

- a hosted cloud service
- a replacement for model-provider billing dashboards
- a replacement for Claude Code/Codex permission systems
- a system-wide process monitor
- a multi-tenant organization/RBAC or generic project-management system
- a controller for external Paperclip, Codex, or Claude processes
- a guarantee that an agent produced a useful outcome

It is a local safety and evidence layer for agent runs you explicitly put under Chronicle control.
