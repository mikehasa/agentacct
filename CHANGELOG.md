# Changelog

All notable changes to agentacct are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.2] — 2026-08-29

Restores claude-code usage ingestion (frozen by a new Workflow-tool journal
row) and surfaces the weekly-plan share as its own receipt row, on top of the
receipt, dashboard, and usage UI refinements merged since 0.10.1.

### Fixed

- Ingestion: accept the Claude Code Workflow tool's `failed` journal row. Its
  new per-agent lifecycle row raised `claude_workflow_journal_schema_drift`,
  which fails closed for the whole home and froze claude-code usage/cost import
  while already-stored sessions kept rendering. A journal row that carries token
  usage still fails closed. (#169)

### Added

- Receipt: a dedicated "Weekly plan" row (macOS app, CLI, TUI) showing the
  Task's calibrated share of its client's weekly plan — calibrated-or-nothing,
  a named calibration state instead of a fabricated number when uncalibrated.
  (#169)

### Changed

- macOS app: packaged builds derive their release version from the same
  `pyproject.toml` used by the CLI and use it as the clone-independent bundle
  version. They embed the exact Git commit and an untracked-aware dirty state;
  the native About panel shows the release and abbreviated source commit without
  adding identity noise to the everyday menu. Distributable builds also reject
  a frozen CLI whose clean-source provenance does not match the app.

## [0.10.1] — 2026-08-27

Work Receipts get an honest exit from red states, readable actions and
checks, per-agent plan visibility, and weekly-plan calibration that finally
lights up — a round of receipt-UI and calibration fixes across the macOS app
and the daemon.

### Added

- macOS app: an in-record back control and Esc both return to the receipts
  list, and the Work tab always lands on the table instead of silently
  re-opening the last record; receipts default to latest-first sorting with
  attention/cost still one click away, shared with the record-mode rail. (#130)
- macOS app: decision words gain color families — claimed done-ish states
  (reported, resolved, mostly done, handed off, finding superseded) wear the
  accent wash, ended-open wears amber (an inferred stop), live states render
  outlined, green stays reserved for machine-verified — plus a status-legend
  popover explaining each word, and `ended_open` now files under a Stopped
  tab instead of "Other". (#130)
- macOS app: the receipt cost dimension shows the token tally (total / fresh /
  cache write / cache read), including for unpriced tasks. (#130)
- Receipt: a blocked Task now says *why* — the newest recorded blocker's own
  words (step, text, next step, staleness) surface under the headline instead
  of a generic statement, on both the list rows and the record page. (#131)
- macOS app: the Actions lists (files / commands / tools) and the Checks rows
  expand in place — the daemon's capped preview stays the collapsed state and
  a height-capped scroll region shows the full, selectable list, so a
  thousand-command receipt is readable without a second-level page. (#131)
- Receipt checks carry detail fields (summary, files, timestamp, artifact
  refs, and an honest "command text not captured for checks" note). (#131)
- Plan: per-Task weekly-plan share (`plan_share`) is stamped on every task
  surface — the sum of the Task's calibrated per-session percentages,
  client-scoped, calibrated-or-nothing — and rendered on the receipt cost
  line, the summary strip, the rail, and the table. (#132)
- macOS app: the Dashboard "Plan and usage" card shows one row per recording
  agent — each with only what it can prove (a provider-reported meter, the
  hatched track when no limit is reported, an amber calibrating chip, and the
  7-day volume/cost) — instead of a single "least headroom" account. (#133)
- Receipt / macOS app: a user resolve lane — `POST /v1/disposition` (the /v1
  lane's first user write; bearer-gated, optimistic-revision, note-required
  resolve) and Mark reviewed / Resolve / Reopen controls on failing checks
  and blockers, so a red finding or blocker has an honest exit. A
  human-resolved finding reads *Finding resolved* and a human-dismissed
  blocker reads *Blocker resolved* (both asserted by the human, never machine
  verification and never a fabricated completion). (#134)
- Plan: codex weekly-plan calibration — codex's meter became week-reset
  cumulative, so it joins the calibratable clients; a dense in-file
  meter-series backfill (idempotent) feeds the fit, and codex tasks now carry
  real weekly shares. (#136)

### Changed

- Plan: an out-of-band calibration fit is accepted when it is persistent and
  split-half-stable (unsticking accounts whose true ratio sits just past the
  trusted band), and a mid-window ratio regime change calibrates on the
  trailing window instead of certifying the blend or reading "calibrating
  forever". (#132, #136)

### Fixed

- Plan: the calibration state that read "calibrating" forever for a
  heavy-usage account whose fit sat just outside the trusted band now
  calibrates on stable or trailing evidence. (#132, #136)

## [0.10.0] — 2026-08-26

The macOS app adopts the v7 brand design system end to end: a semantic
cream/cobalt token palette (light + dark), a nine-role type ramp, evidence
tiers carried by pip shapes everywhere, and the record-page layout for Work
Receipts.

### Added

- macOS app: the Dashboard is rebuilt around work evidence — recent work
  with decision badges and evidence-tier pips, a needs-review card, live
  active work, the provider plan ring, and the daily fresh-token history —
  backed by a deterministic snapshot harness with CI-verified visual
  baselines. (#126)
- macOS app: the Work surface's receipts table (lifecycle filter tabs with
  honest decision-vocabulary buckets, evidence-tier pips with
  checked/checkable ratios, a right-aligned checks rail with failure
  annotations, cost + recency) and the Receipt record page (receipt rail,
  summary strip, dimensions ledger with provenance chips and inline gap
  annotations, checks card, evidence-coverage card with a counted tier
  legend, evidence sources, gaps). (#127)
- macOS app: a Sources pane rendering `/v1/ingestion` — per-source import
  state and recency, continuous-sync watcher state, actionable issues, the
  verifier shelf, and the local-only scope card. (#127)
- Local API: bearer-gated `GET /v1/ingestion`, the `/v1` twin of the legacy
  `/ingestion/health` snapshot, advertised from `/v1/version`. (#127)
- `/v1/tasks` rows now carry `checks_total` / `checks_passed` /
  `checks_failed` from the same reducer the full Receipt uses, so a list
  checks column can never disagree with the open record. (#127)
- macOS app: the Stamped Tile brand mark — app icon (deterministic
  generator script), top-bar lockup, and menu-bar template mark. (#127)

### Changed

- macOS app: Usage becomes a record page (summary strip, one single-series
  daily chart with an optional per-client token filter, By-client and
  By-model tables with proportional share bars, a basis footer); Limits
  gets v7 meters with 75/90% notches, absolute reset times, and named
  states for unreported windows or clients without quota readings. (#127)
- macOS app: green now marks only live-connection facts and independently
  verified evidence — completion claims render in ink, agent-reported check
  passes lose their green mark, and every cost carries its `~`/`≈` prefix
  plus a human basis phrase. Absent facts are named states ("unpriced",
  "not recorded"), never dashes or zeros. (#127)

- README: rebuilt around the redesigned app with five screenshots from a
  fully synthetic, arithmetically consistent demo workspace, plus precise
  wording for the loopback-only local API, evidence-tier vocabulary, and
  the cost grammar. (#128)

### Fixed

- Receipt evidence gaps count only checkable steps: a research/docs step
  can no longer owe a passing check the taxonomy says it cannot have. (#127)
- Check names quoted in per-step grade reasons truncate at word boundaries
  with an ellipsis instead of mid-word. (#127)

## [0.9.4] — 2026-08-25

Fixes Codex session recording for newly-onboarded users: agentacct MCP receipts
recorded by current Codex now link into gradeable work items instead of being
dropped.

### Fixed

- Read Codex's paginated `item_completed` / `McpToolCall` rollout records
  when linking agentacct MCP receipts and deriving Actions, while retaining
  compatibility with legacy Codex rollout carriers. Duplicate carrier
  representations are reconciled by logical call id. A clean failed or unknown
  duplicate does not suppress a valid receipt; malformed carriers, identity
  conflicts, and conflicting successful event ids invalidate that logical call
  and report evidence schema drift. (#123)

## [0.9.3] — 2026-08-23

A metadata release: finishes the agentacct rename on the last user-facing
surfaces the 0.9.1 docs sweep missed. No code or behavior changes.

### Changed
- Renamed the remaining pre-rename "Agent Chronicle" references to agentacct in
  the GitHub issue-form templates, the `LICENSE` copyright line, and the
  `full_demo_task` example. (#121)

## [0.9.2] — 2026-08-22

A documentation release: the docs now match the surface 0.9.1 actually ships.
No code or behavior changes.

### Changed
- Removed internal maintainer docs and docs for already-removed features (the
  public-alpha checklist, the canonical-store design draft, the live-smoke
  guide, the historical-usage-truth policy record, and the connector license
  BOM), and scrubbed the remaining docs, `README.md`, and `INSTALL.md` of
  references to the third-party evidence connectors, the judge/value scoring,
  the `agentacct-claude` / `agentacct-codex` wrappers, the cost proxy, and the
  `agentacct smoke` harness — all removed in 0.9.1 — along with the
  pre-publication "unpublished" framing now that agentacct ships on PyPI.

## [0.9.1] — 2026-08-21

A cleanup release that removes dead code from earlier product pivots. There is
no change to the Work Receipt, usage import, or the coding-agent capture
surfaces; the v1 event-log store and Evidence v2 store are unaffected.

### Removed
- **Unused third-party evidence connectors.** The OpenLIT/OTLP, Paperclip, and
  Entire Git ingestion connectors — which had no producer anywhere in the
  product — along with the `connector` CLI sub-app, the `POST /v1/traces` and
  `/connectors/*/import` routes, and their usage truth-table rows.
- **The run-scoring, wrapper, cost-proxy, and smoke-harness surfaces** of the
  legacy guarded-run lane: the advisory judge/value scoring (the `judge`/`value`
  CLI sub-apps, the `agentacct_prepare_judge`/`agentacct_compute_value` MCP
  tools, and the `/runs/{id}/judge/prepare` + `/runs/{id}/value/compute`
  routes), the `agentacct-claude`/`agentacct-codex` process wrappers, the opt-in
  cost proxy, and the optional real-agent smoke harnesses.
- **The abandoned canonical SQLite store migration.** The never-enabled second
  store (both `AGENTACCT_CANONICAL_LIVE_WRITE` and `AGENTACCT_CANONICAL_READ`
  shipped OFF and it never ran in production) is removed rather than finished.

### Changed
- Finished the rename from the mid-lineage "Agent Chronicle" name and the dead
  `agent-chronicle` CLI to `agentacct` across the public docs, and retired stale
  HTML-dashboard references and two pre-rename smoke logs.

## [0.9.0] — 2026-08-17

The multi-agent release: agentacct now reads and instruments **four** coding
agents — Claude Code, Codex, OpenCode, and Hermes — and the Work Receipt works
across all of them. Ships alongside the first signed, notarized macOS app.

### Added
- **Four coding agents, one Work Receipt.** agentacct now understands Claude
  Code, Codex, OpenCode, and Hermes from their own on-disk stores — tokens, cost,
  sessions, the recorded work, and now WHAT each session did (its commands,
  edited files, and tools) — projected through one client-agnostic Task and
  Receipt model. `agentacct onboard` instruments each installed agent it finds:
  the agentacct MCP tools everywhere, plus each client's native capture surface
  (Claude Code Pre/PostToolUse hooks, a Codex tool-activity + session-end hook, an
  OpenCode observe-only plugin, and Hermes observe-only shell hooks).
- **Receipt Actions derived from a client's OWN transcript — no hook required.**
  A coding agent whose hook does not fire for its built-in tools (Codex, OpenCode)
  now has its Actions recovered at import time from the store agentacct already
  scans for tokens: commands (from Codex's `exec`/`exec_command` and OpenCode's
  `bash`), edited files (from `apply_patch` bodies and OpenCode edit targets,
  cwd-relative — never an absolute prefix), and tool categories + names. Only tool
  names, relative paths, and credential-scrubbed commands are derived; never tool
  output or a file preview. A re-import refreshes a still-growing session in place.
- **Independent checks for OpenCode, from its recorded exit codes.** OpenCode
  records a bash tool's harness exit code on disk, so a recognized test / build /
  lint run is turned into a `client_hook` machine check at import time — lifting a
  check-relevant step from `self-checked` to `independently-checked`, the one
  local signal that is not the agent's own word. A check that fits no eligible
  step stays honestly unattributed rather than credited to unrelated work.
- **Honest Actions provenance — `transcript scan` vs `hook`.** The Receipt now
  names where each session's Actions actually came from: a live client hook, or a
  scan of the client's own transcript/store on disk (Codex/OpenCode, whose hooks
  do not fire). A session captured by both reads both; nothing scan-derived is
  labelled as hook-observed.
- **The Actions dimension: commands, edited files, and specific tool names.**
  Every Receipt now answers "what did this session touch and run" — the paths a
  file-edit tool wrote (cwd-relative, or `~/…` / `../` for a home / out-of-tree
  edit, never an absolute prefix), the single-line credential-scrubbed commands an
  execute tool ran, and the specific tool / connector names (not just coarse
  categories), each capped with an honest overflow disclosure.
- **`handed off` as a first-class lifecycle disposition.** A step whose session
  stopped cleanly at a handoff — or an open step whose session ended without a
  recorded terminal (`ended_open`, inferred from an ambient SessionEnd, the
  weakest provenance and never the agent's word) — is now distinguished from a
  genuine completion on the decision axis.
- **Independent checks from a PostToolUse hook — the only local evidence that is
  not the agent's own word.** When the agent runs a recognized test / build /
  lint / typecheck command, the installed Claude Code PostToolUse hook records
  the exit code the HARNESS observed and projects it as a `client_hook` machine
  check — which lifts that step from `self-checked` (the agent said so) to
  `independently-checked`. Privacy holds the WorkEvent line exactly like the
  tool-category capture: only a coarse check kind, the runner name, and a sha256
  DIGEST of the command are ever recorded — never the command string, its
  arguments, its environment, or its output; an unrecognized or ambiguous
  command is not captured at all. Because the hook sees the command but not which
  recorded step it was for, each check is attached to the step that was active in
  its session when it ran; any that cannot be placed is disclosed as an
  unattributed check in the Receipt's evidence ledger, never guessed onto a step.
  Read-only: the hook never blocks or edits a tool result. Re-run
  `agentacct hooks claude-code install --force` (or `agentacct onboard`) to add
  the PostToolUse entry.
- **The Work Receipt — one canonical answer to "what did this task do, and can I
  trust it".** A new `agentacct.receipt.v1` projects one converged Task (a root
  session with its continuations and subagents) into the eight questions a
  reviewer needs — task, actors, actions, cost, evidence, outcome, gaps, and
  per-field provenance — each dimension carrying where its data came from
  (client log / MCP / hook / CI / git / human) and what could **not** be proven.
  It keeps two axes deliberately separate: *decision status* (what a human or
  agent SAYS happened) and *evidence strength* (how well that is actually
  PROVEN). An agent reporting "done" never raises evidence strength, and a human
  review never counts as machine verification. Read one with
  `agentacct receipt <task>` (or `agentacct receipts` to list them), over the API
  at `GET /v1/receipt?task=` and `GET /v1/tasks`, in the macOS app's new Receipts
  pane, and in the TUI (press `t`). Cost now also carries its *basis* — a local
  client-session figure vs a pricing-table estimate — not just its confidence.
- **Privacy-safe tool-category capture for the Actions dimension.** The installed
  Claude Code PreToolUse hook now records a coarse tool CATEGORY per step
  (read / edit / execute / search / network / agent / plan / mcp / other),
  derived from the tool NAME alone — never its arguments, results, or paths —
  spooled locally and folded into each Task by `agentacct usage import-local`.
  A session with no captured activity shows an honest gap, never a fabricated
  zero.
- **A `/v1` native-shell lane on the local API.** `GET /v1/glance` returns usage
  windows, provider limits, plan-calibration status, and recent sessions in one
  versioned, additive-only payload — computed by the exact same functions
  `agentacct now` and the TUI render. `GET /v1/version` is the daemon/schema
  handshake. Both sit behind a per-boot bearer token; `agentacct serve` publishes
  the actual bound port + token in a 0600 discovery file (`<store>/local-api.json`)
  that a menu bar app or script reads — no configuration, first-alive-writer-wins,
  re-claimed automatically if the slot frees up.

### Changed
- **The Receipt's evidence axis is now honest COVERAGE, not a single grade word.**
  Each step carries an evidence grade — none / claimed / self-checked /
  independently-checked / externally-verified — set by WHO attested it (the
  agent's own report, then a check the harness observed, then a CI/provider
  check), with a one-line reason for why. A Task no longer collapses to one
  categorical grade; it shows the per-tier ratio over its checkable steps
  (e.g. `3/10 self-checked · 7 unchecked`) — the counts are the headline, never a
  percentage that implies more than a count. A Task with no machine-verifiable
  step reads `Not gradeable`, never a fabricated 0. Beneath the ratio an honest
  ledger discloses what it does not cover: steps that ran in subagents, checks
  that attach to no step, non-verifiable steps, and still-open steps. The tier is
  keyed only on a check's trusted source, so an agent naming its own check
  `github_actions` cannot forge external verification. Rendered across the CLI
  (`agentacct receipt` / `receipts`), the TUI, the macOS app, and the
  `/v1/session` per-step wire (each step now carries its grade and each check its
  source).
- **The shared usage view builder moved out of the web module** into
  `usage_view.py`, so the data layer (TUI, `now`, plan calibration) no longer
  imports the web server for pure data logic.
- **Live-view caches now notice the codex namespace bind.** The shared event
  fingerprint behind the TUI's caches and the glance cache folds in the
  source-namespace binding fields, so an in-place TOFU bind refreshes live
  totals immediately instead of waiting for an unrelated event.

### Fixed
- **Subagent sessions in the Work drill-down show a name, not a raw id.** A
  subagent carries no session title, so its row used to read as a bare session
  id until expanded; it now falls back to the subagent's first recorded step
  title.
- **The Work drill-down no longer lists a Task's subagents twice.** Expanding the
  root session re-listed the same subagents that already appear as sibling rows
  in the session tree; the session tree is now the single list.
- **`usage import-local --client claude-code` no longer imports zero sessions
  when a single symlink exists under `~/.claude/projects/`.** A descendant
  directory symlink (e.g. a shared memory directory linked into a project dir, a
  common cross-machine sync pattern) previously aborted the entire Claude
  transcript walk on the first symlinked component, discarding every legitimate
  transcript in the home — while `usage discover-sources` still reported them,
  making the failure baffling. The no-follow policy is unchanged (a directory
  symlink is never traversed, so no foreign subtree is imported); the offending
  link is now skipped, counted as `skipped_unsafe_paths`, and surfaced in the
  import summary instead of silently zeroing the import (reported in #84).

### Removed
- **The HTML browser dashboard.** The web display layer (`/`, `/tokens`, `/raw`,
  `/advanced`, `/control`, the task pages, and their form handlers) is retired in
  favor of `agentacct tui` and the local JSON API — every derived view stays
  available as JSON (`/overview`, `/timeline`, `/sessions`, `/attention`,
  `/usage/summary`, `/evidence/*`, `GET /tasks`, `GET /api/control`), and the
  control plane keeps its full CLI (`agentacct control …`). The recording lanes
  (`/work-events`, `/capture/*`, connectors, `/v1/traces`) are untouched. Also
  retires the migration-era `canonical verify-read-canary` command.

## [0.8.1] - 2026-08-05

### Fixed
- **Claude Code import no longer silently reports `$0.00` at real-world volume.**
  One unreadable or non-transcript file in a large `~/.claude` tree used to flip the
  whole source's usage to "incomplete" and discard it — a confident zero instead of a
  measurement. Import completeness is now per-file: an unresolvable file is excluded
  and surfaced in the diagnostics without withholding the cleanly-parsed sessions
  around it, and `usage import-local` now warns and exits non-zero when it parses
  usage it then withholds instead of reporting a silent `$0`. (Reported by @Evaack, #53.)
- **A file rewritten mid-scan no longer withholds the sessions around it.** A workflow
  journal, or a transcript being appended to during a manual import, is now skipped on
  its own instead of zeroing the whole cohort. Cross-file replay dedup was made
  transactional, so a mid-write file can never leave stray keys that undercount a
  replaying sidechain sibling.
- **`agentacct tui`: the `s`/`u` navigation keys work from every screen.** Pressing
  `u` (usage) or `s` (sessions) from a sub-screen now switches views instead of doing
  nothing — you no longer have to `Esc` back to the home dashboard first.
- **`agentacct tui`: saving a snapshot with `p` no longer smears the live screen.** The
  snapshot export cleared the renderer's dirty regions as a side effect, leaving
  stale/blank cells on some terminals; the screen is now fully repainted afterward.

### Changed
- **`agentacct tui`: the weekly-plan column shows a "calibrating" marker while it warms
  up.** A plan-bearing client whose estimate is still calibrating from your own recorded
  limit history now shows a dim `⋯` (with a "calibrating" note) instead of the same bare
  `—` as a client with no plan — so a fresh install no longer reads as "there is no
  weekly-plan %". Codex, whose meter never yields a weekly-reset %, stays `—`.
- **`agentacct tui`: the sessions row cursor is a calmer muted blue** instead of the
  full-strength primary blue that read as a harsh bright bar.

## [0.8.0] - 2026-08-04

### Added
- **`agentacct tui` just opens for a global install.** The read commands (`tui`,
  `now`, `limits`) now fall back to the machine-wide store when run from a directory
  with no project store, so a global-by-default install no longer needs
  `--store-dir`. Project installs still see their project store; an explicit
  `--store-dir` / `AGENTACCT_STORE_DIR` still wins (and a misconfigured one still
  errors rather than silently reading the global store).
- **A cohesive look and a real nav bar.** The TUI ships a cohesive dark theme and a
  prominent top nav bar (the brand + the current screen) under an accent rule,
  replacing the thin default header — so usage, cost, and work read as one designed
  dashboard.
- **One-key shareable snapshot.** Press `p` from any screen to save a shareable SVG
  of the current view (it renders in any browser or on GitHub) — a quick way to share
  what your agents did. Snapshots are written under the store, never the cwd.
- **The weekly-plan `plan` column on the sessions list.** The full sessions list
  (press `s`) now carries the same weekly-plan % column as the home panel.

### Changed
- **The weekly-plan estimate is shown only when it is honest.** A per-session
  "% of weekly plan" now appears **only when it is calibrated from your own recorded
  limit history**; otherwise the detail shows a "calibrating from your own usage"
  note and the list/home cell shows `—`, instead of a number derived from a shipped
  universal baseline. Providers change how many tokens a plan grants and a single
  shipped equation is a black box, so the figure is grounded in your account or
  withheld. Generalized to every plan-bearing client.
- Refreshed the README screenshots and led the plan-cost section with the unique
  advantage — what fraction of your weekly plan a task consumed — and added
  `scripts/gen_tui_screenshots.py` to regenerate them from synthetic demo data.

## [0.7.0] - 2026-08-04

### Added
- **`agentacct tui` — "≈ X% of your weekly Claude plan" per session.** The home
  **Recent sessions** panel (and the session detail) now estimate what fraction of
  your weekly Claude subscription each task consumed — a `plan` column at a glance. The weekly meter is a per-week-reset cumulative, so a session's share is
  its own weighted usage over the weekly capacity — and because models burn the plan
  at very different rates per dollar, the estimate uses a **measured per-model weight
  table** (shipped, so every user gets a reasonable number out of the box) scaled by a
  **per-account factor** that self-calibrates from your own recorded 7-day usage
  history (from any source — desktop plan-usage, Codex rollouts, or the Claude CLI
  statusLine hook — so CLI-only users are covered). Always labeled a (rough) estimate,
  it only claims a calibrated scale when the fit is trustworthy, and it sharpens as
  more limit history accrues.
- **`agentacct tui` — provider limits on the usage screen, client/model filters,
  and stale-account hiding.** The usage screen (press `u`) now also shows the
  provider plan-limit bars (5h / 7d used %, reset countdown), and `c` / `m` scope
  the whole page — time series, by-model table, and limits — to one client /
  provider or one model. A signed-out or cancelled account, whose last plan reading
  stays frozen forever, is now hidden once it has not updated in over a week (the
  hidden count is disclosed) instead of lingering on the panel with a misleading
  full bar.
- **`agentacct tui` — session status badges, a dedicated usage screen, and a
  recent-sessions panel.** Every session now carries an at-a-glance status badge
  (`▶ in progress` / `⏸ handed off` / `✓ done` / `⚠ blocked`), derived from its
  recorded steps so a cleanly handed-off run reads as handed-off rather than
  still-active — shown in the sessions list, the session-detail header, and a new
  **Recent sessions** panel on the home screen (the five most-recently-active
  sessions, filled in the background so the usage/limits view still paints
  instantly). A new usage screen (press `u`) shows a per-day (or per-week for long
  ranges) token & cost time series with in-terminal bars plus a by-model
  breakdown; `d` cycles the range (7d / 30d / 90d / all). All from the same
  authoritative local event log the rest of the TUI reads — no credentials, no
  API calls.
- **Client integrations re-sync themselves after an upgrade (`agentacct sync`).**
  Onboarding wrote a client's MCP config, hooks, and instructions once; a later
  agentacct upgrade left them stale (the source of the outdated-`record_section`
  guidance). The activation record now stamps the agentacct version that wrote the
  integration, and `agentacct start` re-syncs the recorded clients when that stamp
  is behind the installed version — re-running the same idempotent onboard writers,
  so it never clobbers your own settings (a custom statusLine is left alone). A new
  `agentacct sync` forces the refresh on demand. This keeps a machine's MCP /
  instructions / hooks current with the installed version instead of drifting.
- **`agentacct tui` freshens usage on launch and refresh.** The TUI now imports
  usage from the client session logs in the background when it opens and on `r`
  (the same scan the HTML dashboard did on Refresh), so the session you're in —
  and a session you just started — show real token usage instead of zero. Runs
  off the refresh timer, in a worker, and only when enabled (so it never scans on
  every tick).
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

### Fixed
- **`agentacct_record_section` guidance now states its required arguments.** The
  onboarding/instruction prose named `section_title` / `section_status` but not
  that `source` and a stable `section_id` are also required, so some agents (e.g.
  Codex) omitted them and the call failed. The shared instruction line — used by
  the live MCP server instructions, the Claude Code SessionStart context, and the
  on-disk `CLAUDE.md` / `AGENTS.md` — now lists all required args with a concrete
  example.

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

[Unreleased]: https://github.com/mikehasa/agentacct/compare/v0.10.2...HEAD
[0.10.2]: https://github.com/mikehasa/agentacct/releases/tag/v0.10.2
[0.10.1]: https://github.com/mikehasa/agentacct/releases/tag/v0.10.1
[0.10.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.10.0
[0.9.4]: https://github.com/mikehasa/agentacct/releases/tag/v0.9.4
[0.9.3]: https://github.com/mikehasa/agentacct/releases/tag/v0.9.3
[0.9.2]: https://github.com/mikehasa/agentacct/releases/tag/v0.9.2
[0.9.1]: https://github.com/mikehasa/agentacct/releases/tag/v0.9.1
[0.9.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.9.0
[0.8.1]: https://github.com/mikehasa/agentacct/releases/tag/v0.8.1
[0.8.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.8.0
[0.7.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.7.0
[0.6.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.6.0
[0.5.3]: https://github.com/mikehasa/agentacct/releases/tag/v0.5.3
[0.5.2]: https://github.com/mikehasa/agentacct/releases/tag/v0.5.2
[0.5.1]: https://github.com/mikehasa/agentacct/releases/tag/v0.5.1
[0.5.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.5.0
[0.4.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.4.0
[0.3.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.3.0
[0.2.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.2.0
[0.1.0]: https://github.com/mikehasa/agentacct/releases/tag/v0.1.0
