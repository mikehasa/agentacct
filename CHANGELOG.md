# Changelog

All notable changes to agentacct are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

[Unreleased]: https://github.com/mikehasa/agentacct/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.1.0
