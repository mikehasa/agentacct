# Agent Chronicle workflow instructions

Use these instructions in Hermes skills, OpenCode `AGENTS.md`, OpenClaw skills/agent identity, Claude Code `CLAUDE.md`, Codex `AGENTS.md`, or any MCP-capable coding agent.

## Purpose

Agent Chronicle should be a workflow ledger for AI-agent work, not only a command wrapper. When Chronicle MCP tools are available, the coding agent should record a small, objective timeline of meaningful work.

## Required behavior

When the client exposes local identifiers, first attach context:

```text
Call sentinel_attach_client_context with:
  source: <client-or-agent-name>
  client: <codex|claude-code|hermes|opencode|openclaw|other>
  client_session_id: <local session/thread id>
  client_transcript_id: <local transcript/log id or path stem if known>
  parent_client_session_id: <parent/root session id if this is a child agent>
  turn_id/message_id/request_id: <current id if known>
  client_event_timestamp: <client timestamp if known>
```

How to obtain the join ids (be honest when you cannot):

- Chronicle attributes imported usage to your work only through exact `client_session_id` or `client_transcript_id` matches. `project_dir` and `run_id` alone group events but never attribute usage.
- Claude Code: the session id and transcript path are provided to hooks (stdin JSON `session_id` / `transcript_path`), not to the model or MCP servers. Install the Chronicle hook bridge (`agent-chronicle hooks claude-code install`, then merge BOTH the "hooks" and "env" blocks from the example settings — the `env` block's `ENABLE_TOOL_SEARCH=auto` keeps the Chronicle tools directly callable instead of deferred): the hook captures the current session's ids at session start (SessionStart) and on every tool call (PreToolUse) and persists them to `client-context/claude-code.json` under the project store, and sections automatically inherit them as client-derived join keys — no need for the model to know its own ids. The context file stores identity fields only: the session id, the transcript file stem, and a project basename label — never full local paths, tool input, prompts, or secrets. If a hook, wrapper, or the user gives you the session id explicitly, passing it still yields the strongest (exact) attribution. The imported `client_transcript_id` is the transcript file stem under `~/.claude/projects`.
- Codex: the thread id is not exposed in-band. Attach whatever you have (`project_dir` at minimum) and expect the attach response to report `join_hint_quality: weak`.
- Never guess or fabricate ids. A missing id is diagnosable; a wrong id is silent mis-attribution.

Context inheritance: sections recorded without explicit ids inherit them from, in priority order, (1) the Claude Code hook context file (client-derived — captured by the hook from the client itself) and (2) the last successful `sentinel_attach_client_context` on the same MCP server process (agent-reported). Inherited keys are recorded in `metadata.client_context_inherited_keys` with `client_context_source` (server-authored; forged values are stripped on every other recording path), and the (session, transcript) id pair always comes from a single source: if you pass either id explicitly, ids are never inherited — the pair is yours to complete. Inherited ids are fail-safe and never `exact`: hook-captured ids attribute at `high` confidence (`client_derived_*` strategies — client-derived and TTL-fresh, but not bound to the recording MCP session), attach-inherited ids at `medium` (`inherited_*` strategies — freshness unproven). Exact attribution requires ids passed explicitly on the section call, and explicitly passed ids always override inherited ones. Attach again at the start of every NEW conversation (including after `/clear` or resume). Run `agent-chronicle mcp doctor` to check whether recorded context and the hook bridge are joinable (read-only; it never writes to the ledger).

Before meaningful work:

```text
Call sentinel_record_section with:
  section_id: <short stable id for this piece of work>
  section_status: started
  section_title: concise task description
  source: <client-or-agent-name>
  run_id: <stable task/session id if known>
```

During work:

- Record checkpoints after important decisions, handoffs, or scope changes: call `sentinel_record_section` again with the same `section_id` and `section_status=checkpoint`.
- Sections are the work contract: use `section_status=started`, `checkpoint`, `completed`, or `blocked`, and include the same client/session/turn identifiers when known.
- If the client exposes visible token/cost usage during the session, call `sentinel_record_agent_usage_debug` with `reporting_basis=visible_client_usage` and the same client/session/turn identifiers. If the client does not expose usage, call it with `reporting_basis=unavailable`; do not invent token or cost numbers.
- Record failures/blockers instead of hiding them.
- After tests, builds, lint, smoke tests, or browser checks, record machine-check evidence with `sentinel_record_machine_check` when available; otherwise use `sentinel_record_event` with a compact result summary.

At completion:

```text
Call sentinel_record_section with:
  section_id: <same section id>
  section_status: completed
  source: <client-or-agent-name>
  run_id: <same task/session id>
  summary: what changed, with tests/builds/diffs/tool calls actually observed
```

If blocked:

```text
Call sentinel_record_section with:
  section_id: <same section id>
  section_status: blocked
  source: <client-or-agent-name>
  run_id: <same task/session id>
  blocker: concrete blocker
  next_step: what would unblock it
```

## Claim boundaries

Keep these claims separate:

- MCP events prove that the agent recorded work in Chronicle.
- MCP client context and section events prove the agent reported semantic workflow anchors and local join keys.
- MCP agent-usage debug events prove only what the agent said it could see about itself. They are comparison evidence and do not update Chronicle usage/cost totals.
- Local usage import proves that Chronicle parsed client-reported token data from an implemented local store path.
- Provider/API proxy data proves only the traffic that actually flowed through Chronicle or returned provider usage/cost fields.

Do not claim exact subscription billing or hard budget enforcement unless the run used a supported importer/proxy/enforcement path.

## Client-specific notes

### Hermes

Hermes can use this as a real skill and can call Chronicle MCP tools directly when configured with:

```bash
hermes mcp add agent-chronicle --command agent-chronicle --args mcp serve --store-dir .agent-sentinel/state
```

For one-shot runs, load the skill explicitly when available:

```bash
hermes chat -s agent-chronicle-workflow -q "..."
```

### OpenCode

OpenCode should receive these instructions through repo `AGENTS.md` or a custom OpenCode agent. Use `--format json` in smoke/validation runs when token/cost evidence is needed, because OpenCode emits structured step-finish token and cost fields.

### OpenClaw

OpenClaw should receive these instructions through an OpenClaw skill or agent identity. Confirm the actual workspace/repo path before reading files. If the workspace is not the target repo, record a blocker or workspace-mismatch event rather than pretending inspection succeeded.

## Minimal smoke prompt

```text
Use Agent Chronicle MCP to record a section with sentinel_record_section (section_status=started). Inspect the integration docs if available. Record the same section with section_status=completed and one objective finding in the summary. Reply exactly SENTINEL_WORKFLOW_OK.
```
