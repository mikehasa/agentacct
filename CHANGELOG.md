# Changelog

All notable changes to agentacct are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.2] - 2026-07-31

### Fixed
- Global `agentacct onboard` no longer strands your history. When an existing
  populated global store is present, onboarding now reuses that ledger instead
  of silently pointing your clients at a fresh empty store (the new XDG location
  is used only on a clean machine). Onboarding also warns about the two surfaces
  it cannot rewrite for you — an OpenCode registration and a Claude Code hook
  left on a different store. (#26)

### Changed
- Readability cleanup of `activation.py` (the first-run readiness funnel) and
  `agent_capabilities.py` (the client capability matrix): clearer names, added
  documentation, and more unit coverage — no behavior change to the shipped
  configuration. Thanks to @FZ2000. (#27, #28)
- Re-tightened lock-file permissions on every acquisition (an `os.open` created
  with the right mode does not re-secure a pre-existing loosened file), and
  corrected several docstrings whose security/durability claims did not match
  the code. (#29)

## [0.5.1] - 2026-07-30

### Changed
- `AGENTACCT_*` is now the primary environment-variable prefix (for example
  `AGENTACCT_STORE_DIR`). The pre-rename `AGENT_CHRONICLE_*` and `AGENT_SENTINEL_*`
  names stay accepted forever; when two aliases are set to different values,
  agentacct refuses rather than silently pick one. Env writers export all three
  names into child processes so existing scripts keep working. (#24)

## [0.5.0] - 2026-07-30

### Changed
- The MCP tools were renamed from `sentinel_*` to `agentacct_*` (for example
  `agentacct_record_section`); the server now exposes only the `agentacct_*` names.
  **After upgrading, re-run `agentacct onboard` (or `agentacct setup instructions`)**
  so your `CLAUDE.md` / `AGENTS.md` instruction block uses the new tool names, then
  restart your coding-agent client. Your recorded history is unaffected: the
  pre-rename `sentinel_*` names stay recognized in historical logs, and stored
  event data is unchanged. (#22)
- The internal Python package was renamed `agent_chronicle` → `agentacct`, and the
  MCP server now advertises the name `agentacct`. Instruction-block markers now
  write `agentacct`; the pre-rename `agent-chronicle` / `agent-sentinel` markers
  stay recognized, so an existing managed block migrates in place on the next
  setup run. (#22)

## [0.4.0] - 2026-07-29

### Changed
- `agentacct onboard` now installs **once, machine-wide, by default** (`--scope global`)
  and writes **zero files into your repository**. It registers user-level MCP servers
  (`~/.claude.json`, `~/.codex/config.toml`), installs the Claude Code hook wrapper outside
  the store, adds the standing "record your work" instructions, and merges the hook block
  into `~/.claude/settings.json` only with your consent (`--yes` or an interactive prompt).
  The previous per-repository behavior is still available with
  `agentacct onboard --scope project`. Registrations embed the absolute `agentacct` path so
  GUI-launched desktop clients (which do not inherit your shell `PATH`) can launch the
  server. (#20)
- The default global store moved to an XDG-standard location:
  `$XDG_STATE_HOME/agentacct/state` (`~/.local/state/agentacct/state`). Older global stores
  under `~/.agent-sentinel-global/` are still recognized, and a store that already holds
  recorded data is preferred, so upgrading never blanks a populated dashboard. Fold an old
  store into the new one with `agentacct usage merge-store` when you want a single ledger.
  (#20)

## [0.3.0] - 2026-07-29

### Added
- OpenCode usage now imports from the native `opencode.db` session store. Per-session
  tokens (input / output / reasoning / cache) and the model are read directly from the
  authoritative `session` rollup, so an interactive OpenCode session shows token usage
  without an exported `run --format json` file. The database is opened read-only and a
  corrupt store fails closed. When OpenCode records no cost, cost is estimated from tokens
  (labeled as an estimate, never a fabricated figure); OpenCode's `-fast` routing suffix
  (e.g. `gpt-5.6-sol-fast`) is normalized to the base model so it prices against the
  published rate. The legacy exported-JSON path is kept as a fallback. (#18)

### Changed
- The local dashboard's default port (8765) auto-advances to the next free port when it is
  busy — the dashboard no longer fails to start just because a port is taken — and prints
  the port it actually bound. An explicit `--port` is still honored strictly: if that exact
  port is occupied the command fails with a clear message instead of silently moving. (#16)

### Fixed
- `agentacct mcp serve` no longer exits when it cannot resolve a store. A server that exits
  at startup reads to the host agent (OpenCode / Claude Code / Codex) as a crash, which was
  the most likely cause of reports that agentacct "crashed" a client. The MCP server now
  stays connected in a degraded mode — answering the handshake and returning a clear
  JSON-RPC error on any recording call — and never silently creates or picks a store. MCP
  setup previews also tell users to remove any stale pre-rename (`agent-sentinel` /
  `agent-chronicle`) server registration first, which is the other crash vector. (#17)
- The OpenCode usage importer skips non-object JSON records, so one stale export file can no
  longer abort usage discovery for every client during onboarding. Thanks to @ZPVIP. (#15)

## [0.2.0] - 2026-07-27

### Added
- Activity-first overview: the homepage feed is ordered newest-first, with open
  findings and blockers pinned in a "Needs attention" strip above a "Recent
  activity" timeline. A recently-run session now shows even before any work is
  attributed, instead of being buried under attribution-first ordering.
- Inline finding controls: resolve / mark reviewed / reopen render directly on
  the card (in the Needs-attention strip and on workspace findings) instead of
  inside a collapsed details expander, so a finding is one click from resolved.
- `/sessions` is a time-first full-history browser: a newest-first / attributed
  order toggle, a "Recorded work only" filter, and "Show more" pagination past
  the default page.
- The Task page's evidence inventory lists each redacted work record and check
  (type, result, time, and source) instead of only counts; raw session and
  transcript ids stay in the local forensic API.
- `agentacct --version` prints the installed version.
- CSS-only breakdown tabs on the Overview usage chart — the By agent / By model /
  By agent-model selector switches without JavaScript or a page reload; the
  `?usage_breakdown=` deep link still sets the initially-selected tab.
- OSS front matter: CI/PyPI/Python/license badges, this changelog, and a CI test
  matrix across Python 3.11, 3.12, and 3.13 (all green on the Linux CI runner).

### Changed
- Install docs: the README notes how to get `pipx` (or use `uv`) before the first
  install step, and the Global install recipe adds a CLI-less MCP registration
  path for desktop-app clients that ship no `claude` / `codex` CLI.
- `/health` now reports `service: agentacct-local-api`; the readiness check and the
  read canary still accept the pre-rename `agent-sentinel-local-api` value, so
  cross-version upgrades keep recognizing the dashboard.

### Fixed
- The Claude Code hook wrapper is installed under `.claude/hooks/` instead of inside
  the store directory, and its subcommands fail open. Moving or renaming the store
  can no longer make a `"*"` PreToolUse hook fail closed and block every tool call
  in a running session. Doctor still recognizes pre-relocation installs; re-running
  `hooks claude-code install --force` migrates them.

## [0.1.0] - 2026-07-25

### Added
- Initial public release. Local-first Agent Work Intelligence for coding agents:
  imports client-reported token usage from local session files, records MCP work
  context (sections, machine checks), joins the two with honest confidence labels,
  and shows it on a local dashboard (`agentacct serve`). Ships the `agentacct`,
  `agentacct-claude`, and `agentacct-codex` console scripts. Local-first,
  observe-only, no telemetry, no provider API keys. Python ≥ 3.11 on macOS / Linux.

[Unreleased]: https://github.com/mikehasa/agentacct/compare/v0.5.2...HEAD
[0.5.2]: https://github.com/mikehasa/agentacct/releases/tag/v0.5.2
[0.5.1]: https://github.com/mikehasa/agentacct/releases/tag/v0.5.1
[0.5.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.5.0
[0.4.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.4.0
[0.3.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.3.0
[0.2.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.2.0
[0.1.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.1.0
