# Changelog

All notable changes to agentacct are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`agentacct tui` sessions drill-down — folded subagents, roles, and a
  restructured detail.** The sessions list now shows only top-level sessions —
  child/subagent sessions are folded under their parent (a `⋔ sub` count column),
  so a run with dozens of subagents is one row instead of dozens. Opening a
  session shows a **Subagents** section listing each child with its role and task,
  read on the fly from the child's local transcript (`attributionAgent` +
  the Task prompt) — no credentials, no re-import. The session detail is
  restructured from a wall of text into a header panel plus one collapsible per
  step (status + title collapsed; expand for the summary and colored check
  results). Refresh feedback is clearer: the sessions rebuild shows a loading
  indicator, and a manual `r` on the dashboard flashes the refreshed-stamp.
- **`agentacct now` — a current usage & cost snapshot.** One glance at calendar
  windows (today / last 7 days / last 30 days / all time) with token totals,
  estimated cost, and session counts, plus a by-client and top-models breakdown
  for a chosen window (`--window`, default 7d) and a compact provider-limit
  teaser. Read from the authoritative local event log (no credentials, no API);
  costs are agentacct's token-based estimates, not provider billing — a window
  that isn't fully priced is shown as a partial `~$` subtotal rather than a
  misleading exact figure. Supports `--json` and `--client`.
- **`agentacct limits` — provider-reported usage limits, read from local files.**
  A new foundation records real rate-limit snapshots as `rate_limit_observed`
  events and renders them: for each client, the 5-hour and weekly (7-day) windows
  with the provider's own used-percentage, reset countdown (when the provider
  reports one), plan, and credits. Data is read **passively from local files —
  no credentials, no API calls, no CLI scraping**:
  - **Codex** from `~/.codex/sessions/**/rollout-*.jsonl` (`rate_limits` on
    `token_count` events: used percent, window length, reset time, credits, plan).
  - **Claude Code (desktop app)** from `~/Library/Application Support/Claude/plan-usage-history.json`
    (the desktop app's rolling 5-hour / 7-day utilization series; Pro/Max only).
  - **Claude Code (terminal CLI)** via a lightweight `statusLine` command
    (`python -m agentacct.statusline_hook`) that Claude Code feeds its live
    5-hour / 7-day utilization — the only local surface for CLI-only users, who
    have no desktop plan-usage file. The hook is tiny and fail-open (it never
    slows or breaks the status bar); it writes the reading to a spool the usage
    import ingests, and also prints a compact status bar (model · context% ·
    5h/7d · cost). `agentacct onboard` installs it into `~/.claude/settings.json`
    without ever overwriting a status line you already configured.
  Snapshots are captured as a no-cost side effect of `agentacct usage import-local`
  and `agentacct usage watch`, and recorded idempotently — an unchanged limit
  re-observed on every scan is a no-op, so a new event is written only when a
  percentage, window, or credit value actually changes. `agentacct limits`
  supports `--json` and `--client`. Reading limits is best-effort and never fails
  a usage import.
- **`agentacct tui` — a live terminal dashboard (Textual).** A full-screen,
  auto-refreshing view over the same authoritative local event log as
  `agentacct now` / `agentacct limits` (no credentials, no API calls): calendar-window
  usage & cost (today / 7d / 30d / all), a by-client and top-models breakdown,
  and provider rate-limit bars with a live reset countdown. Keys: `r` refresh
  now, `w` cycle the breakdown window, `q` quit. It polls the event log on an
  interval (`--refresh`, default 5s) and recomputes only when the log actually
  grew, while a one-second tick keeps the reset countdowns and freshness ticking.
  The status line shows a `refreshed HH:MM:SS` stamp so a manual `r` is always
  visible even when the numbers are unchanged. Press `s` for a **sessions
  drill-down**: a list of sessions (title, project, tokens, cost, step counts)
  that you select into to see that session's work steps and their machine-check
  results. The work ledger it reads is expensive, so it is built once on demand
  in a background thread (never on the refresh timer) and cached until the log
  changes.
  Supports `--window` and `--client`; requires an interactive terminal (for
  scripting, use the `--json` forms of `now` / `limits`). Adds `textual` as a
  dependency.

### Changed
- `agentacct now`, `agentacct limits`, and the new `agentacct tui` now share a
  single internal data layer (`agentacct.usage_snapshot`), so the three surfaces
  derive usage, cost, and rate-limit readings from one place and can never
  disagree. No change to `now` / `limits` output.

## [0.6.0] - 2026-08-01

### Added
- **agentacct now keeps its event ledger in SQLite by default.** The event log
  (`events.sqlite3`) is the authoritative store: a fresh install is SQLite-only
  from its first event, and an existing `events.jsonl` store auto-adopts the log
  the next time it is opened — reconciling the log from the flat file, then
  leaving that file behind as a backup. The upgrade is safe even with daemons
  running: while an older, not-yet-restarted process keeps appending to
  `events.jsonl`, the new code drains those straggler writes into the log, so a
  rolling upgrade never splits events between the two ledgers or loses one.
  Reads and writes no longer re-parse a growing text file on every access, the
  foundation for a fast local CLI and live view. Three commands give explicit
  control over the flat file:
  `agentacct event verify-log` proves the SQLite copy matches a flat file line
  for line; `agentacct event drop-flat-ledger --confirm` deletes the leftover
  `events.jsonl` backup once a store has cut over; and `agentacct canonical
  rebuild-store` (re)builds the SQLite usage index from the ledger. The cutover
  is durable — a persistent store marker keeps it in effect across restarts with
  no environment variable, so no later open can revert or wipe it — and it fails
  loud rather than ever serving an empty or half-migrated store. Set
  `AGENTACCT_EVENT_LOG_AUTHORITATIVE=0` to opt back into the legacy flat-file
  mode, where `events.jsonl` stays authoritative and the SQLite log is a proven
  mirror.
- `agentacct canonical rebuild-store` rebuilds the canonical SQLite index
  (per-day usage and per-task rollups) directly from your event ledger in about
  a second, so the fast usage/cost read path has a store to serve even on a
  machine that never ran the background writer.

### Removed
- The owner-gated offline **Evidence v2 rebuild subsystem** (`agentacct evidence
  rebuild` — snapshot / build-candidate / activate / rollback and their modules)
  is gone. It reconstructed the Evidence v2 store by replaying the v1 ledger from
  a sealed snapshot, and it read that ledger from `events.jsonl` — an assumption
  the SQLite-default cutover broke. Evidence v2 itself is unchanged and still
  self-heals its projection from its own durable spool; only the offline
  replay-from-flat-ledger DR/activation tooling was retired.

### Fixed
- `agentacct mcp doctor` now reports **store writability** for a SQLite-backed
  store. The writability probe previously ran only when a flat `events.jsonl`
  existed, so once a store used the SQLite log (now the default) the doctor
  silently dropped that diagnostic. It now checks the log's writability without
  writing any bytes, keeping the doctor read-only while restoring the check for
  every store.
- The canonical usage importer no longer counts non-usage events as usage. Any
  event whose type merely contained the substring "usage" — most importantly
  `agent_usage_debug_reported`, the debug snapshot that is explicitly *not*
  billing truth — or that merely carried the estimated-token fields set to empty
  (ordinary `task_completed`, `task_started`, `machine_check` rows, and more)
  was imported as a usage measurement. On one real ledger that was 187 non-usage
  events that would have inflated the SQLite index's token and cost totals.
  Usage is now anchored on the one real usage event type, `model_usage`.

## [0.5.3] - 2026-07-31

### Added
- Local logs now shows the recording calls agentacct refused, with counts and a
  fixed set of reason codes. A refused call previously left no trace anywhere —
  no store entry, no counter, no log line — so an agent that failed to record
  was invisible to you and to the maintainer, while the dashboard told you to
  record more work. The figure is derived at read time from data already on
  disk, so it covers refusals that predate this release, and it stores only
  counts and reason codes, never the offending value or path.

- Work now carries honest partial and stopped states instead of collapsing to
  all-or-nothing. A task no longer reads "in progress" just because one step was
  left open: if you demonstrably moved on — kept working in other sessions for a
  day while this one sat untouched — it reads "mostly done"; if you have not been
  active anywhere since, it stays "in progress" (being away is never treated as
  abandonment). A new `handed_off` section status lets an agent record a clean
  stop when you continue in a new session, as a terminal state rather than a live
  one. Tasks also expose a partial verification count ("3 of 5 steps verified")
  rather than a single verified/unverified flag.

### Changed
- The "usage without work context" prompt no longer blames you for all of it;
  it now points at the refused-recording list for the part agentacct caused.

### Fixed
- Secret redaction no longer destroys the record it was protecting. Any value
  containing something that looked like a credential was replaced *in its
  entirety* with `[REDACTED]`, and the detector fired on ordinary words — the
  api-key pattern matched inside `task-`, `disk-` and `risk-`, and the bearer
  pattern matched the English phrase "Bearer token". On one real ledger that
  destroyed 117 project paths, 20 section ids, 5 summaries and 2 idempotency
  keys; because every casualty collapsed onto the same literal string,
  unrelated sections merged into one phantom section. Redaction now replaces
  only the matched span, and every redaction records which field was cut and
  which pattern class cut it — a repair you cannot see is a bug.
- Redaction coverage is now broader than before, not narrower. Patterns are
  split into three independent families — prefixed vendor tokens (GitHub,
  GitLab, AWS, Google, Slack, Stripe, npm, PyPI, Hugging Face, Linear,
  DigitalOcean and more, matched anywhere, with or without a `Bearer`),
  `Authorization`/`Proxy-Authorization` headers including their JSON and
  `curl -H` serializations, and the genuinely ambiguous bare `Bearer <token>`
  — so a guard added for one can no longer disable another. The ambiguous
  family was calibrated against a real ledger rather than guessed: zero false
  positives across 27k distinct strings. The shapes still missed are named in
  the module, with the method to re-derive them.
- `agentacct_record_section` now accepts `title` as an alias for
  `section_title`. The recording contract agentacct ships to every client told
  agents to send `title`, which the schema then rejected outright, losing the
  whole record; the instruction is corrected and the alias keeps
  already-onboarded machines working without re-running `onboard`.
- Limit errors now say what was received, not only what is allowed. An agent
  that overshoots a length limit could previously only shrink blindly and
  retry.
- Metadata size is measured in real UTF-8 bytes on every write surface. It was
  measured against an ASCII-escaped encoding, so each CJK character counted as
  6 bytes and each emoji as 12 — a Chinese-writing agent was refused at roughly
  a third of the advertised budget, by an error naming a parameter it had never
  sent. Size errors now name the field that actually overflowed.
- `files` entries: the project-relative rule is published in the schema (it was
  enforced but documented nowhere), and an absolute path that provably lies
  under the call's own `project_dir` is normalized instead of failing the whole
  call. This was the single largest cause of refused recordings. Paths that
  escape the project are still rejected.
- A tool call mangled in transit — parameters absorbed into a narrative field
  as literal text — is now flagged with a warning and a marker on the stored
  record instead of being kept silently. It never rejects and never repairs.
- A check that failed and was then fixed no longer shows as a standing,
  unresolved "Agent finding". The retire-on-rerun logic keyed on the exact
  command, and fixing a build almost always edits the command, so the passing
  re-run never superseded the failure. A later same-scope pass now demotes the
  failure out of Needs attention into a "resolved in a later check" state —
  still visible, counted on its own, and one-click reinstatable — never hidden
  and never upgraded to Verified. Findings whose check exited 0 (a check
  asserting a defect) and passes from another project/session are never demoted.

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
