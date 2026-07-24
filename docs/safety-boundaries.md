# Safety Boundaries

Agent Chronicle is intentionally conservative in the early alpha.

The core rule is:

```text
Only control work that Chronicle explicitly starts and records.
```

## What Chronicle does

Chronicle can:

- start a command under Chronicle ownership
- record stdout, stderr, runtime metadata, and reports
- pause/resume/kill Chronicle-owned process groups
- detect simple repeated-error and timeout patterns
- record cost events for model/API calls routed through Chronicle surfaces
- prepare isolated judge packages
- run opt-in, budget-gated OpenRouter judge scoring
- compute advisory value scores from existing evidence
- expose safe local API and MCP tools
- append metadata-only multi-source evidence to a local durable spool
- parse explicitly supplied OpenLIT/OTLP, Paperclip, and Entire Git evidence through read-only adapters

## What Chronicle does not do

Chronicle does not:

- scan your machine for existing agent processes
- attach to existing Hermes, Claude Code, Codex, Cursor, OpenCode, or other agent sessions
- pause, kill, or inspect processes it did not start
- modify global Claude Code/Codex/Hermes configuration by default
- upload local run logs to a hosted service
- copy prompts, responses, thoughts, tool bodies, or transcripts into metadata-only Evidence v2 capture
- mutate Paperclip, OpenLIT, Entire refs, or another upstream system through a connector
- call paid judge models unless explicitly requested
- store API keys in the run ledger
- guarantee that a run created useful value

## Paid API safety

Paid model calls are opt-in.

For provider forwarding or judge scoring, use a low-limit key and a local budget cap:

```bash
agent-chronicle judge run latest \
  --model openai/gpt-4o-mini \
  --max-total-usd 0.01
```

Recommended safeguards:

- use a dedicated test key
- set a provider-side account cap
- set a Chronicle-side per-request or per-run budget depending on the surface
- start with cheap or free models
- review generated judge packages before using them for important decisions

For `agent-chronicle judge run`, `--max-total-usd` is a per-request preflight budget for that judge call. It checks the estimated judge request before sending it upstream; historical cost events in the same local store do not consume this per-request limit.

The cost proxy is local-only by default and refuses non-local bind hosts. Do not expose it on `0.0.0.0` or a public interface without adding authentication and an intentionally reviewed deployment boundary.

## Secret handling

API keys should come from environment variables such as:

```text
AGENT_CHRONICLE_OPENROUTER_API_KEY
```

Do not paste real keys into docs, reports, issues, or PR comments.

Chronicle reports and ledgers should contain metadata and cost events, not raw secrets.

## Report sharing safety

JSON reports, local API responses, MCP report responses, and judge packages are local/private artifacts by default.

They may include:

- stdout/stderr tails
- command arguments
- local absolute artifact paths
- task summaries copied from local logs

Review or redact these artifacts before sharing them outside your trusted local environment.

## MCP safety

The MCP server currently exposes safe local tools only:

- list runs
- get reports
- record machine-check evidence
- prepare judge package
- compute value score from existing evidence

It does not expose a paid `sentinel_run_judge` tool.

This is intentional: agent integrations should be able to inspect evidence without silently spending money.

MCP tool arguments are validated before dispatch. Invalid limits, missing required fields, malformed run IDs, and non-positive budgets return MCP invalid-params errors instead of generic server crashes.

## Hook safety

Hook support is an adapter layer, not the core safety model.

The Evidence v2 capture adapters for Claude Code, Codex, and Cursor accept at
most 1 MiB of JSON, allowlist metadata, retain only workspace-relative paths,
and never persist prompt/response/thought/tool argument/tool result/stdout/
stderr fields. Their hook entrypoint is fail-open. `capture manifest` only
renders an opt-in fragment and reports its target path; it does not read or
write active host settings.

The Claude Code hook installer writes project-local files:

```text
.agent-sentinel/hooks/claude_pre_tool_use.py
.claude/settings.agent-sentinel.example.json
```

Review the example settings before copying them into any active agent runtime configuration.

Hook commands must work without your shell profile PATH. Claude Code sessions (especially the desktop app) often run hook commands in a minimal environment where a virtualenv-only `agent-chronicle` or a bare `python` is not resolvable. The installer therefore embeds absolute paths resolved at install time: the wrapper records the absolute path of the running `agent-chronicle` executable (with a bare-name PATH fallback for relocated projects), and the example settings invoke the wrapper with the absolute path of the installing Python interpreter. The default project install addresses the wrapper via `$CLAUDE_PROJECT_DIR`; with `--user-settings-example` (the global-install path) the printed and written example settings instead address the wrapper by its absolute path — `$CLAUDE_PROJECT_DIR` is a project-scope variable that does not exist for user-level `~/.claude/settings.json`.

The wrapper fails open: if no `agent-chronicle` executable can be started, or the CLI exits without producing a decision, or the wrapper itself hits an unexpected error, the wrapper prints an explicit `allow` decision and exits 0. A broken or moved Chronicle install must never break — or silently block — the user's tool calls; the capture layer is an adapter, not a gate. `agent-chronicle hooks claude-code doctor` reports hook executables that cannot resolve, covering the wrapper, the example settings, and any active `.claude/settings.json` / `.claude/settings.local.json`. The generated hook command and the doctor's command analysis assume a POSIX shell (macOS, Linux, WSL); doctor flags Windows-style commands as unverifiable rather than guessing.

## Connector and OTLP safety

- `/v1/traces` and `/capture/*` are protected by the existing localhost guard.
- Capture payloads are bounded to 1 MiB; connector JSON is bounded to 4 MiB.
- OpenLIT/OTLP attributes pass through a metadata allowlist; OTLP is a
  transport, not automatic billing authority.
- Paperclip input is an exported snapshot. Chronicle has no Paperclip
  credential or mutation method.
- Entire reads only known public refs, commit metadata/trailers, and allowlisted
  metadata scalars using non-mutating Git commands. It does not change refs,
  index, or worktree and does not open transcript/prompt/message blobs.
- Control signals are advisory evaluations. The API and CLI always report
  `external_action_dispatched=false`.

OpenLIT is Apache-2.0; Paperclip and Entire are MIT at the pinned commits in
[integration-license-bom.md](integration-license-bom.md). Chronicle copies no
upstream implementation code in these adapters.

## Value-score safety

The value score is advisory.

It combines:

- judge deliverable score
- cost efficiency
- machine-check evidence
- risk penalties

It should not be treated as proof that work was valuable. Human review remains the final decision layer.

## Prompt optimization safety

Prompt/context optimization can save tokens, but it can also remove important constraints if done carelessly.

Any future prompt optimizer should preserve:

- explicit safety constraints
- budget limits
- secrets-handling rules
- file paths and commands
- acceptance criteria
- things the user said not to do

The safe default should be preview-first, not automatic replacement.
