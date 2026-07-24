# Live MCP Client Smoke Results

This page records sanitized maintainer evidence for testing Agent Chronicle *inside* real Claude Code and Codex MCP client sessions.

This is different from `docs/live-smoke-results.md`, which verifies that Chronicle can launch real Claude Code/Codex processes. The tests here verify whether real agent clients can discover and call Agent Chronicle's MCP tools from inside their normal tool environment.

Historical note: the dated results below were observed pre-rename, under the `agent-sentinel` binary and registration key. Commands, configs, and client transcripts are preserved exactly as recorded — dated evidence keeps the name under which it was observed.

## 2026-06-27 release-gate result

Goal:

```text
real Claude Code or Codex client
→ Agent Chronicle configured as MCP server
→ client calls sentinel_get_event_summary
→ client can approve the MCP tool call in the normal interactive interface
→ local dashboard /events/summary has aggregate Sentinel event data available
```

Result:

```text
Claude Code interactive MCP client: passed
Codex interactive MCP client: passed
```

Root cause of the earlier blocked result:

```text
Claude Code and Codex sent raw JSON-RPC objects over stdio.
Agent Chronicle originally only supported Content-Length framed stdio messages.
Sentinel waited for headers that never arrived, while the clients waited for initialize responses.
Both clients timed out before exposing Sentinel tools.
```

Fix validated in this run:

```text
Agent Chronicle now supports both stdio framings:
- Content-Length framed JSON-RPC
- raw JSON-RPC object framing used by the tested Claude Code/Codex clients
```

## Local Sentinel MCP and dashboard behavior

Verified locally without paid model calls:

- `agent-sentinel mcp serve` responds correctly over stdio Content-Length frames.
- `agent-sentinel mcp serve` responds correctly to raw JSON-RPC stdio frames.
- `initialize` echoes the MCP client's requested `protocolVersion` for client compatibility.
- `tools/list` exposes event tools, including `sentinel_record_event`, `sentinel_attach_client_context`, `sentinel_record_section`, `sentinel_record_agent_usage_debug`, and `sentinel_get_event_summary`.
- MCP-recorded `model_usage` events aggregate through `/events/summary`.
- MCP-recorded semantic context and section events aggregate through `/events/summary`.
- The local dashboard shows aggregate integration events, sources, providers, estimated cost, and estimated input/output tokens.

## 2026-07-02 semantic context dogfood result

Goal:

```text
prompt-first setup in an isolated local repo
→ project-local Sentinel state and MCP config
→ fresh real Codex and Claude Code clients load Sentinel MCP
→ clients call semantic context and section tools across multiple user turns
→ local event summary shows semantic events without token/cost overclaiming
```

Result:

```text
Codex semantic MCP transport: passed
Claude Code semantic MCP transport: passed
```

Verified semantic event types:

```text
client_context_attached
section_started
section_checkpoint
section_completed
task_completed
```

Dogfood finding and fix:

```text
Local editable installs may not put `agent-sentinel` on the future agent client's PATH.
If MCP config writes `command = "agent-sentinel"` in that state, a fresh client may not expose Sentinel tools.
The setup flow now supports `--mcp-command <path>` and auto-detects the current executable path when appropriate.
```

Claim boundary:

```text
These semantic MCP events prove the clients could report workflow context.
They intentionally do not prove token usage, cost, subscription billing, or automatic monitoring.
Usage and cost evidence still require local usage import or a provider/proxy path.
```

## 2026-07-02 usage-debug dogfood result

Goal:

```text
fresh real Codex and Claude Code clients load Sentinel MCP
→ clients call sentinel_record_agent_usage_debug across multiple user turns
→ clients report visible token/cost fields only if actually exposed by the client
→ unavailable usage is recorded as explicit comparison evidence instead of guessed numbers
```

Result:

```text
Codex usage-debug MCP transport: passed
Claude Code usage-debug MCP transport: passed
```

Observed usage visibility:

```text
Codex: usage unavailable inside the tested client session
Claude Code: usage unavailable inside the tested client session
```

Verified usage-debug event behavior:

```text
agent_usage_debug_reported events were recorded by both clients.
Both clients used reporting_basis=unavailable because no token/cost counter was visible to the agent during the chat.
No numeric token or cost fields were guessed.
```

Claude Code sanitized evidence:

```text
Claude Code reported a visible local client identifier and used it consistently.
Claude Code recorded client_context_attached, section_started, section_checkpoint, section_completed,
three agent_usage_debug_reported events, and task_completed.
All three usage-debug snapshots used reporting_basis=unavailable.
```

Codex sanitized evidence:

```text
Codex recorded client context, section events, usage-debug reports, and completion.
Codex reported usage unavailable and did not edit files during the smoke.
```

Claim boundary:

```text
These usage-debug MCP events prove that agents can record whether usage was visible to them.
They do not prove token usage, cost, subscription billing, or provider-billed spend.
For tested Codex and Claude Code chat sessions, local usage import remains the source for token snapshots.
```

## Claude Code client result

Attempted client path:

```bash
claude --debug
```

with project-local `.mcp.json`:

```json
{
  "mcpServers": {
    "agent-sentinel": {
      "command": "/path/to/agent-sentinel",
      "args": ["mcp", "serve", "--store-dir", "$STATE"]
    }
  }
}
```

Observed result: passed.

Interactive evidence:

```text
Claude Code detected the project MCP server.
Claude Code prompted to approve the project MCP server.
Claude Code exposed agent-sentinel-sentinel_get_event_summary as an MCP tool.
Claude Code prompted before running the tool.
After approval, Claude Code called the tool successfully.
Claude Code replied that the Sentinel MCP tool was available and the call succeeded.
```

Sanitized result line:

```text
Sentinel MCP tool was available and the call succeeded (returned a summary of 1 event).
```

## Codex client result

Attempted client path:

```bash
codex
```

with project-local `.codex/config.toml`:

```toml
[mcp_servers.agent-sentinel]
command = "/path/to/agent-sentinel"
args = ["mcp", "serve", "--store-dir", "$STATE"]
startup_timeout_sec = 8
```

Observed result: passed.

Interactive evidence:

```text
Codex trusted the temporary project directory.
Codex started the configured agent-sentinel MCP server.
Codex exposed agent-sentinel.sentinel_get_event_summary as an MCP tool.
Codex prompted before running the tool.
After approval, Codex called the tool successfully.
Codex printed the returned event summary and replied that the tool was available and the call succeeded.
```

Sanitized result line:

```text
Sentinel MCP tool was available, and the sentinel_get_event_summary call succeeded.
```

Important nuance:

```text
codex exec can expose the tool, but non-interactive exec mode may cancel MCP tool calls because there is no interactive user approval prompt. The real interactive Codex TUI path was required to approve and complete the tool call.
```

## Current public claim

Safe claim:

```text
Agent Chronicle's MCP server and local dashboard event aggregation work locally. Agent Chronicle has been smoke-tested as an MCP tool inside real interactive Claude Code and Codex sessions on the maintainer VPS.
```

Still avoid claiming:

```text
Agent Chronicle automatically tracks every Claude Code/Codex session without configuration.
Agent Chronicle reads exact Claude Code/Codex/ChatGPT subscription billing.
MCP-reported token/cost values are exact unless the reporting integration provides exact provider usage.
```

## Safety notes

- These live-client tests consume real Claude Code and Codex/ChatGPT account usage.
- Prefer zero-token protocol probes before repeating live-client tests.
- Keep raw artifacts local and sanitized; do not publish temp paths, agent-run identifiers, process IDs, account identifiers, or raw logs containing credentials.
