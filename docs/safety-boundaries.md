# Safety Boundaries

agentacct is intentionally conservative in the early alpha.

The core rule is:

```text
Only control work that agentacct explicitly starts and records.
```

## What agentacct does

agentacct can:

- start a command under agentacct ownership
- record stdout, stderr, runtime metadata, and reports
- pause/resume/kill agentacct-owned process groups
- detect simple repeated-error and timeout patterns
- record cost events for model/API calls routed through agentacct surfaces
- expose safe local API and MCP tools
- append metadata-only multi-source evidence to a local durable spool

## What agentacct does not do

agentacct does not:

- scan your machine for existing agent processes
- attach to existing Hermes, Claude Code, Codex, Cursor, OpenCode, or other agent sessions
- pause, kill, or inspect processes it did not start
- modify global Claude Code/Codex/Hermes configuration by default
- upload local run logs to a hosted service
- copy prompts, responses, thoughts, tool bodies, or transcripts into metadata-only Evidence v2 capture
- store API keys in the run ledger
- guarantee that a run created useful value

## Secret handling

Do not paste real keys into docs, reports, issues, or PR comments.

agentacct reports and ledgers should contain metadata and cost events, not raw secrets.

## Report sharing safety

JSON reports, local API responses, and MCP report responses are local/private artifacts by default.

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

MCP tool arguments are validated before dispatch. Invalid limits, missing required fields, and malformed run IDs return MCP invalid-params errors instead of generic server crashes.

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

Hook commands must work without your shell profile PATH. Claude Code sessions (especially the desktop app) often run hook commands in a minimal environment where a virtualenv-only `agentacct` or a bare `python` is not resolvable. The installer therefore embeds absolute paths resolved at install time: the wrapper records the absolute path of the running `agentacct` executable (with a bare-name PATH fallback for relocated projects), and the example settings invoke the wrapper with the absolute path of the installing Python interpreter. The default project install addresses the wrapper via `$CLAUDE_PROJECT_DIR`; with `--user-settings-example` (the global-install path) the printed and written example settings instead address the wrapper by its absolute path — `$CLAUDE_PROJECT_DIR` is a project-scope variable that does not exist for user-level `~/.claude/settings.json`.

The wrapper fails open: if no `agentacct` executable can be started, or the CLI exits without producing a decision, or the wrapper itself hits an unexpected error, the wrapper prints an explicit `allow` decision and exits 0. A broken or moved agentacct install must never break — or silently block — the user's tool calls; the capture layer is an adapter, not a gate. `agentacct hooks claude-code doctor` reports hook executables that cannot resolve, covering the wrapper, the example settings, and any active `.claude/settings.json` / `.claude/settings.local.json`. The generated hook command and the doctor's command analysis assume a POSIX shell (macOS, Linux, WSL); doctor flags Windows-style commands as unverifiable rather than guessing.

## Capture and control safety

- `/capture/*` is protected by the existing localhost guard.
- Capture payloads are bounded to 1 MiB.
- Control signals are advisory evaluations. The API and CLI always report
  `external_action_dispatched=false`.

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
