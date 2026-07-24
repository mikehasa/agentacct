# Security Policy

Agent Chronicle is an early-alpha local developer tool. It starts from conservative defaults because it can launch and control child processes and can optionally proxy model-provider requests.

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

Agent Chronicle is local-first:

- It stores run state on the local filesystem.
- It does not include telemetry.
- It does not store provider API keys in ledgers, reports, or config.
- API keys must be supplied through environment variables for commands that explicitly need them.
- The dashboard/API bind to localhost by default.
- Provider forwarding is disabled by default.
- Real provider forwarding requires an explicit provider allowlist and local `--max-total-usd` budget cap.
- MCP tools intentionally expose safe local report/event/value primitives and do not expose paid judge calls.

## Process-control boundary

Agent Chronicle should only control processes it starts and records as Chronicle-owned.

It must not scan for, attach to, pause, resume, kill, or inspect existing Claude Code, Codex, Hermes, Cursor, OpenCode, shell, or other unrelated agent processes.

If you find a path that lets Agent Chronicle control an unowned process, treat it as a security bug.

## Secret handling

Agent Chronicle redacts common secret-shaped keys and values before persisting local integration events. Integrations should still send only the minimum useful evidence.

Do not put these values in event metadata, reports, issue comments, screenshots, or test fixtures:

- API keys
- authorization headers
- OAuth tokens
- private keys
- raw provider request/response bodies that may contain user data
- private repository content beyond a minimal reproduction

## Localhost services

The local dashboard/API are unauthenticated and intended for trusted localhost clients. Do not expose them on `0.0.0.0`, a public interface, or a tunnel without adding authentication and reviewing the data being served.

## Supported versions

Agent Chronicle is currently early alpha. Security fixes target the latest `main` branch until versioned releases exist.
