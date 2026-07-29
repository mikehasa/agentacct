---
name: agent-chronicle-workflow
description: Use when working in a repo with agentacct MCP configured, or when asked to track coding-agent work, smoke-test agentacct integrations, or report objective AI-agent task evidence.
version: 1.0.0
author: agentacct
license: MIT
metadata:
  hermes:
    tags: [agent-chronicle, coding-agent, mcp, workflow, finops]
    related_skills: [hermes-agent, opencode, github-pr-workflow]
---

# agentacct Workflow

## Overview

Use agentacct as a lightweight workflow ledger for AI-agent work. When the agentacct MCP tools are available, record a compact timeline of meaningful work rather than relying only on final chat summaries.

This skill does not mean agentacct automatically sees every token or exact provider bill. MCP events, local usage imports, and provider/proxy cost enforcement are separate capabilities.

## When to Use

Use this skill when:

- Working in a repository that has agentacct initialized.
- agentacct MCP tools such as `agentacct_record_event` are available.
- Running Hermes, OpenCode, OpenClaw, Claude Code, Codex, or another coding agent with agentacct integration.
- The user asks for evidence of AI-agent work, token/cost tracking, MCP smoke tests, or Agent FinOps workflow validation.

Do not use this skill to claim exact billing unless the run used a supported usage importer or provider/proxy path.

## Workflow

1. If local client identifiers are available, call `agentacct_attach_client_context`:

```text
source: hermes or the active coding-agent name
client: hermes, claude-code, codex, opencode, openclaw, or other
client_session_id: local session/thread id
client_transcript_id: local transcript/log id if known
parent_client_session_id: parent/root session id if this is a child agent
turn_id/message_id/request_id: current ids if known
client_event_timestamp: client timestamp if known
```

2. Before meaningful work, open a section with `agentacct_record_section`:

```text
section_id: short stable id for this piece of work
section_status: started
section_title: concise task description
source: hermes or the active coding-agent name
run_id: stable task/session id if known
```

3. During work, record important checkpoints (`agentacct_record_section` with the same `section_id` and `section_status=checkpoint`):

- major decisions
- scope changes
- handoffs between agents
- repeated errors
- blockers

Sections are the work contract. Use `section_status=started`, `checkpoint`, `completed`, or `blocked`, and include client/session/turn identifiers when known.

4. If visible token/cost usage is available, call `agentacct_record_agent_usage_debug`:

```text
reporting_basis: visible_client_usage
source/client/client_session_id: same values used for context when known
provider/model: visible provider and model if known
input_tokens/output_tokens/cache_read_input_tokens/reasoning_output_tokens/cost_usd: only fields actually visible
```

If the client does not expose token/cost usage, call the same tool with `reporting_basis: unavailable` and a short summary. Do not guess.

5. After tests/builds/smokes, record machine-check evidence:

- Prefer `agentacct_record_machine_check` when available.
- Otherwise call `agentacct_record_event` with a compact test/build result.

6. At completion, close the section with `agentacct_record_section`:

```text
section_id: same section id
section_status: completed
summary: what changed, with tests, builds, diffs, tool calls, token/cost evidence actually observed
```

7. If blocked, call `agentacct_record_section` with `section_status=blocked`:

```text
section_id: same section id
section_status: blocked
blocker: concrete blocker
next_step: what would unblock it
```

7. In the final response, report:

- what was changed or tested
- exact validation command/result
- agentacct event summary if checked
- token/cost data only if actually observed
- unsupported claims or blockers clearly labeled

## Claim Boundaries

Keep these separate:

- MCP events prove that the agent recorded work.
- MCP client context and section events prove the agent reported semantic workflow anchors and local join keys.
- MCP usage debug events prove only what the agent reported seeing about its own token/cost usage. They are comparison evidence and are not agentacct usage/cost totals.
- Local usage import proves agentacct parsed supported client-reported token data.
- Provider/API proxy data proves only traffic that actually flowed through agentacct or returned provider usage/cost fields.

Do not say agentacct hard-stopped, billed exactly, or tracked all sessions unless the relevant enforcement/import/proxy path was actually used.

## Client Notes

### Hermes

If agentacct MCP is configured, call agentacct tools directly. For one-shot work, load this skill explicitly:

```bash
hermes chat -s agent-chronicle-workflow -q "..."
```

### OpenCode

OpenCode should receive equivalent instructions through repo `AGENTS.md` or a custom OpenCode agent. For smoke tests, use `--format json` when token/cost fields are needed.

### OpenClaw

Confirm the actual workspace path before reading repo files. If OpenClaw is running from an isolated workspace that does not contain the repo, record a blocker/workspace mismatch instead of pretending file inspection succeeded.

## Minimal Smoke Prompt

```text
Use agentacct MCP to record a section with agentacct_record_section (section_status=started). Inspect the integration docs if available. Record the same section with section_status=completed and one objective finding in the summary. Reply exactly SENTINEL_WORKFLOW_OK.
```

## Verification Checklist

- [ ] `agentacct_record_section` was called with `section_status=started`.
- [ ] `agentacct_attach_client_context` was used when local ids were available.
- [ ] `agentacct_record_agent_usage_debug` was called with visible usage or `reporting_basis=unavailable`.
- [ ] Meaningful checkpoints or machine checks were recorded when applicable.
- [ ] Completion or blocker was recorded.
- [ ] Final response separates MCP evidence from token/cost/billing evidence.
- [ ] No secrets or raw provider bodies were printed.
