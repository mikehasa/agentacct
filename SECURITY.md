# Security Policy

agentacct is an early-alpha local developer tool. It starts from conservative defaults because it can launch and control child processes and can optionally proxy model-provider requests.

## Reporting a vulnerability

Please report security issues privately before opening a public issue.

Preferred path: use GitHub's **Report a vulnerability** flow for this repository if it is available under the Security tab. If that flow is not available, open a public issue titled `Security contact request` without vulnerability details so the maintainer can provide a private channel.

In the private report, include:

- affected version or commit
- operating system
- exact command or integration path
- expected behavior
- observed behavior
- minimal reproduction steps
- whether any secrets, API keys, logs, or local files may have been exposed

Do not include real API keys or private logs in a public issue.

## Security model

agentacct is local-first:

- It stores run state on the local filesystem.
- It does not include telemetry.
- It does not store provider API keys in ledgers, reports, or config.
- API keys must be supplied through environment variables for commands that explicitly need them.
- The local API binds to localhost by default.
- Native-shell `/v1/*` routes require a per-boot bearer token stored in the
  owner-only `<store>/local-api.json` discovery file.
- Provider forwarding is disabled by default.
- Real provider forwarding requires an explicit provider allowlist and local `--max-total-usd` budget cap.
- MCP tools intentionally expose safe local report, event, and workflow-evidence
  primitives; they do not expose paid provider calls.

## Process-control boundary

agentacct should only control processes it starts and records as agentacct-owned.

It must not scan for, attach to, pause, resume, kill, or inspect existing Claude Code, Codex, Hermes, Cursor, OpenCode, shell, or other unrelated agent processes.

If you find a path that lets agentacct control an unowned process, treat it as a security bug.

## Secret handling

agentacct redacts common secret-shaped keys and values before persisting local integration events. Integrations should still send only the minimum useful evidence.

Do not put these values in event metadata, reports, issue comments, screenshots, or test fixtures:

- API keys
- authorization headers
- OAuth tokens
- private keys
- raw provider request/response bodies that may contain user data
- private repository content beyond a minimal reproduction

## Localhost services

The API has two compatibility lanes with different controls:

- Native-shell `/v1/*` routes are bearer-gated. The daemon writes the actual
  port and per-boot token to `<store>/local-api.json` with owner-only permissions;
  a missing configured token fails closed, and a missing or invalid bearer is
  rejected.
- Legacy JSON routes remain available to trusted localhost clients without
  bearer authentication. They are still protected by loopback binding, a
  localhost-only `Host` guard, and `Origin` checks for browser mutations.

Do not bind the service to `0.0.0.0`, expose it through a public interface, or
put it behind a tunnel without adding an appropriate authentication boundary
and reviewing the data being served.

## Supported versions

agentacct publishes versioned releases. Security fixes are developed on the
current `main` branch and shipped in a new release; users should reproduce and
report against the latest published version and upgrade from older early-alpha
releases.
