# Public Alpha Checklist

agentacct is early alpha. Use this checklist before recommending a public release or broad public announcement.

This document is intentionally public-safe: it covers install, use, safety, tests, and integration claims. It should not include private strategy, commercial roadmap, fundraising plans, or enterprise sales material.

Legend: `[x]` verified done, with terse evidence (phase / PR). `[ ]` genuinely open. Items labeled "release gate" are re-verified at the Phase 4 (rename + publish) gate even when the underlying work exists today.

## 1. First-time user doorway

The README should let a new developer understand and try the project without private context.

Required:

- [x] Clear one-line positioning: local-first Agent Work Intelligence for coding agents — usage truth, recorded work, and honest joins. — Phase 3 README rewrite (the retired "black box recorder / circuit breaker" framing is gone from public docs).
- [x] Status is marked early alpha. — README status block (Phase 1).
- [x] Install path matches the actual distribution state. — Current private-build correction: README/INSTALL require an owner-supplied authorized local checkout; the deleted public repository and unofficial mirrors are never presented as install sources.
- [x] While unpublished, README never says `pipx install agentacct` or advertises a public Git URL. It uses editable install commands against the authorized local checkout.
- [x] 30-second demo uses only local commands. — `agentacct demo` runs on a throwaway temp store (Phase 1, PR #53).
- [x] Demo does not require provider API keys. — Phase 1.
- [x] Demo does not make paid provider/API calls. — Phase 1; it records zero provider cost events.
- [x] README explains localhost dashboard safety. — README safety defaults; non-local hosts rejected since Phase 1, Origin/Host guard added in Phase 3.
- [x] README distinguishes what works today from what is not automatic yet. — Phase 3 README rewrite ("What works today" / "What it does not claim yet").
- [x] README leads with a private-build prompt backed by the local INSTALL.md and `agentacct init`; it explicitly requires an authorized source checkout.
- [x] The dashboard's first impression matches the docs: three primary destinations (Work / Usage / Advanced), with Sessions owned by Work and Raw/Evidence inspectors owned by Advanced. Work is one deduplicated feed; explicit blockers are user actions, while failed checks are open findings owned by the work/agent unless an explicit user blocker is recorded. Attribution and source hygiene stay in diagnostic views. Usage owns detailed token charts; only `/raw` runs the live scan. Fresh stores show a product empty state, stable routes remain available, and old `/?tab=` links still redirect. Activity sync separately shows ready/manual/live/degraded ingestion state and gives a concrete recovery action instead of a bare warning. — Phase 7.1 product home + Phase 9 trusted ingestion (rendered-HTML pins in dashboard and ingestion-health tests).

Smoke command:

```bash
agentacct demo
```

Expected properties:

- creates an agentacct-owned run
- captures stdout/stderr
- records objective machine-check evidence
- writes a report
- prints dashboard/report follow-up commands
- records zero provider cost events

## 2. Local-first safety

Required defaults:

- [x] No telemetry. — design invariant; stated in README safety defaults.
- [x] No provider API keys required for basic use. — observe-only setup path (Phase 1.5).
- [x] No provider API keys stored in reports, ledgers, docs, or fixtures. — secret redaction on record; changed-file secret scan in section 4.
- [x] Dashboard/API bind to `127.0.0.1` by default. — `serve` / `api serve` / `cost proxy` reject non-local hosts (Phase 1).
- [x] Cross-origin browser requests are rejected. — Phase 3: localhost-guard middleware on the dashboard API and the cost proxy (Host allowlist on every request; mutating methods reject cross-origin and null Origin with 403 before any handler; repeatable `--allow-host` opt-out).
- [x] OS support claims match the POSIX-only code. — Phase 3: pyproject classifiers plus README/INSTALL requirements say macOS/Linux with Windows via WSL only; on win32 the CLI exits 2 with a one-line WSL pointer instead of an fcntl traceback.
- [x] Store resolution never silently picks a hidden default. — env var → project walk-up (with worktree remap) → exit 2; the legacy silent `~/.agent-sentinel` fallback is retired (Phase 1 Batch 4). Global-install mode uses `$HOME/.agent-sentinel-global/state` with an explicit absolute `--store-dir` embedded in every registration (Phase 2.8 / INSTALL.md global section).
- [x] Provider forwarding is off by default. — opt-in proxy path only.
- [x] Real provider forwarding requires an explicit budget cap. — proxy refuses without one.
- [x] agentacct only controls processes it starts and records as agentacct-owned. — README "What it does not claim yet".
- [x] MCP tools do not expose paid judge/provider calls. — MCP surface is record/read tools only.

Smoke commands:

```bash
agentacct doctor
agentacct serve
agentacct mcp doctor
agentacct usage health --json
```

Review:

- `doctor` should explain next steps without printing secret values.
- `serve` should use localhost by default.
- `mcp doctor` is read-only by default; `--write-probe` round-trips a test event in a throwaway temp store, never the project ledger.
- `agentacct mcp workflow-smoke --json` should exercise initialize → tools/list → record event → list event → summarize events without paid APIs, against a throwaway temp store by default (it records real events; pass `--store-dir` only when you deliberately want them in a persistent store).
- `agentacct-claude` and `agentacct-codex` should be documented as explicit opt-in wrappers around agentacct-owned runs, not automatic monitoring or exact subscription billing.
- `agentacct cost subscription-estimate` should clearly label subscription allocation as approximate, require a user-entered subscription price, and return `subscription_unavailable` instead of inventing exact billing when no denominator is provided.
- `agentacct usage import-local --dry-run --json` should scan local client session files for usage fields, not read provider keys, call provider APIs, or persist raw transcript content.
- `/health` should keep `ok` (local API liveness) separate from `ingestion_status`; `/ingestion/health` and `usage health --json` should expose the same per-source receipts, watcher heartbeat, issues, and actions without scanning client logs.

## 3. Project-local onboarding

Project-local setup should be safe before any global/agent config is modified.

Required:

- [x] `agentacct init` creates project-local policy/state and gitignore entries. — Phase 1.
- [x] `agentacct init --agent codex` adds project-local agent workflow instructions and previews MCP setup. — Phase 1/1.5.
- [x] `agentacct init --agent codex --write-mcp` writes only supported project-local MCP config. — Phase 1.5.
- [x] `agentacct setup prompt --agent codex` prints a copy/paste prompt that requires the owner-supplied checkout and standard observe-only setup; `--full` is self-contained except for that checkout path.
- [x] `--mcp-command <path>` is available for local checkout/venv testing when `agentacct` is not on the future agent session PATH. — Phase 1.
- [x] Project state defaults to `.agent-sentinel/state/` after init. — Phase 1.
- [x] Local env files are gitignored. — Phase 1.
- [x] Claude Code/Codex setup helpers preview by default. — Phase 1.
- [x] Setup helpers embed the ABSOLUTE local store path in written config by default, on purpose: GUI-launched clients do not inherit shell environment variables, so correctness beats portability; `--relative-store-path` is the documented opt-out for deliberately shareable config. — Phase 1 Batch 4 (this replaces the older "avoid absolute paths" rule, which had pinned a stray-store bug).
- [x] Setup docs say onboarding helpers are not a guarantee that every agent runtime automatically uses agentacct. — INSTALL.md per-client capability matrix + README.

Smoke commands:

```bash
agentacct init
agentacct init --agent codex
agentacct setup prompt --agent codex
agentacct policy validate
agentacct doctor
agentacct setup mcp --agent claude-code
agentacct setup mcp --agent codex
```

## 4. Tests and static checks

Required before a public-alpha recommendation (release gate — rerun at the Phase 4 gate, never tick in advance):

- [ ] Full suite green at the release commit:

```bash
git diff --check
.venv/bin/python -m pytest tests/ -q --tb=short
```

- [ ] Changed-file secret scan over docs, examples, fixtures, and source changes.

Check for:

- API keys
- access tokens
- private keys
- raw provider request/response bodies
- private logs
- local absolute paths in shareable examples

## 5. Provider/API claims

Public docs must be precise.

Allowed claims when true:

- dry-run cost proxy routes exist
- provider cost estimates are local estimates unless provider-reported actuals are available
- Claude Code/Codex token usage can be imported from local client session stores when those clients expose usage fields; label it `client_reported`, not exact subscription billing
- imported cost confidence is `client_reported` only when the client itself reports a cost figure, `estimated_from_tokens` when `--estimate-costs` priced it from the local pricing table, and `unknown` otherwise
- the CLI importer and `usage watch` record each session once at first observation and never update it by default; only the dashboard **Refresh** action or an importer run with `--refresh` replaces re-observed rows with fresh totals — never imply the watcher keeps totals live by default
- persisted imports expose per-source attempt/success timestamps, parse/skip/error counts, watermark, and limit diagnostics; a successful manual receipt is not proof that a continuous watcher is alive
- one continuous watcher owns each store through a lease/heartbeat; duplicate live watchers are rejected, stale/lost leases are recoverable, and the watcher must be restarted after an agentacct upgrade because a running Python process does not hot-reload new importer code
- Codex `--limit-sessions` counts complete root conversation groups rather than flat child/review rows, and an internal row inherits a model only from its exact recorded parent; otherwise the model remains unknown
- OpenRouter/OpenAI/DeepSeek forwarding paths are opt-in and budget-gated
- Anthropic/Gemini validation coverage exists, but direct forwarding is not enabled if that is the current state

Avoid:

- implying exact subscription billing for Claude Code/Codex unless verified through exposed usage data
- implying automatic monitoring of tools not launched or integrated through agentacct
- treating `GET /health` `ok=true`, a successful manual scan, or historical source coverage as proof that continuous ingestion is live
- implying real provider forwarding is enabled by default
- implying the local dashboard is safe to expose publicly without authentication

### Join honesty claims

The join between imported usage and recorded work is the core product promise; public docs must keep these claims exactly:

- [x] Missing beats wrong: when the ids do not prove a link, the dashboard shows the gap instead of inventing an attribution. — Phase 1 (PR #53) shared `join_rules` matcher drives every attribution surface.
- [x] Inherited ids are never `exact`: only ids passed explicitly on the recording call earn `exact`; hook-inherited and log-evidenced context tops out at high. — Phase 1 / Phase 2.7.
- [x] Conflicting id claims veto the attribution (both sides) instead of splitting or guessing. — Phase 1; extended to client-log evidence in Phase 2.7.
- [x] Refusal over guessing on ambiguity: a section evidenced by more than one session links to none; ambiguous hook contexts inherit nothing and record a server-authored refusal reason. — Phase 1 Batch 3 / Phase 2.7.
- [x] Every attribution carries a labeled confidence (`exact`/`high`/`medium`/`low`) on the dashboard. — Phase 2 (PR #55).
- [x] Join coverage uses the complete matching store, not the bounded response sample. Summary payloads expose recent-window `result_scope`, bridge `detail_scope`, full-store coverage ratios, and stable degraded reasons; capped detail arrays are explicitly partial. — Phase 9 trusted ingestion.

## 6. Claude Code and Codex claims

Before broad public-alpha claims, run live smoke tests for Claude Code and Codex setup paths where possible. Maintainer release-gate smoke tests should prove all of the following:

- the real agent binary starts successfully
- agentacct launches the agent as the owned child process
- agentacct writes `metadata.json`, `stdout.log`, `stderr.log`, and `report.md`
- the expected model response appears in `stdout.log`
- the run exits successfully without requiring global config changes

Minimal smoke pattern:

```bash
agentacct smoke claude-code
agentacct smoke codex
agentacct smoke all --json
```

`agentacct smoke ...` is a maintainer/release-gate command, not a default CI step. Run it only when real Claude Code/Codex credentials and a small paid-token budget are available. See [`docs/live-agent-smoke.md`](live-agent-smoke.md) for evidence fields, equivalent explicit commands, and troubleshooting.

- [ ] Rerun the paid live smoke gate (`agentacct smoke all --json`) at the Phase 4 release gate.

MCP-inside-agent support needs a separate live-client release gate: real Claude Code/Codex must discover agentacct MCP tools, call event/semantic tools such as `agentacct_record_event`, `agentacct_attach_client_context`, `agentacct_record_section`, `agentacct_record_agent_usage_debug`, or `agentacct_get_event_summary`, and the local dashboard must aggregate those usage/context/debug events. As of the latest attempt, interactive Claude Code and Codex sessions exposed and successfully called Sentinel MCP tools after user approval, including usage-debug snapshots that correctly recorded unavailable visible usage instead of guessed token/cost numbers.

- [x] Claude Code records MCP work context reliably with the documented three-lever recipe: the SessionStart hook delivers the record-your-work directive, `ENABLE_TOOL_SEARCH=auto` in the settings `env` block keeps the agentacct tools directly callable, and the hook bridge supplies real session/transcript join ids. — Phase 2.10 (PR #60); proven live by the owner compliance test (un-primed neutral-project session recorded a section, rendered Attributed on the dashboard).
- [x] Codex sessions gain session-linked work context via client-log evidence at usage import: agentacct pairs recorded events with the Codex session log that created them (creation responses only; high confidence, never `exact`; multi-session evidence refuses). — Phase 2.7.
- [x] Codex import limits are root-aware: parent lineage is resolved from a bounded session-meta prefix before `--limit-sessions`, the limit counts root conversation groups, all discovered descendants of selected roots remain together, and full JSONL token/evidence parsing is limited to those selected groups. Missing parents stay orphan groups; lineage is never guessed from time or working directory. — Phase 9 trusted ingestion.
- [x] Codex internal workflow labels are never treated as models. A missing internal model may inherit only from its exact recorded parent with provenance; otherwise it remains unknown and unpriced, with no scan-wide fallback. — Phase 9 trusted ingestion.
- [x] Instrumentation markers separate pre-install history from post-install behavior: CLI-provenance-gated pre/post markers drive the dashboard's post-install context-rate KPI, so backfilled history renders neutrally instead of as failure. — Phase 2.10 (PR #60).
- [x] A trusted mechanical-only hook observation creates one bounded homepage Task/session even when MCP did not fire, while models/checks remain observations and no token, cost, or named-work meaning is fabricated. Generic manifests remain manual adapter primitives rather than complete automatic integrations. — Session observation product bridge.
- [x] Discovery, onboarding, import, and watch resolve the same bounded Codex/Claude source-home plan under explicit, custom env, and XDG paths; source namespaces prevent identical client ids from silently crossing homes. — Trusted multi-source ingestion.
- [ ] Live zero-token/rollout-only sessions remain visible without fabricating usage.

Local client usage import is the preferred non-wrapper path for normal Claude Code/Codex sessions. It should read summarized usage fields from local client session stores, store one agentacct event per imported session (per model lane), redact prompt-like titles by default, and label cost confidence honestly: `client_reported` only when the client itself reports cost, `estimated_from_tokens` when `--estimate-costs` priced it locally, and `unknown` otherwise. By default each session is recorded once at first observation; the dashboard **Refresh** action and the `--refresh` importer flag are the only paths that replace re-observed rows with fresh totals. Persisted scans must also leave durable per-source receipts; watcher liveness comes only from the current lease/heartbeat, not from the existence of usage rows.

Use careful wording:

```text
agentacct wraps commands it launches and provides project-local onboarding helpers for coding-agent workflows.
```

Avoid claiming:

```text
agentacct automatically tracks all Claude Code/Codex usage.
```

unless the integration path has been verified and documented. Even after the release-gate smoke passes, do not imply agentacct automatically monitors Claude Code/Codex sessions launched outside agentacct.

## 7. Public docs language scan

Public files should focus on OSS developer value:

- install
- first run
- safety defaults
- current limitations
- CLI/API/MCP usage
- contribution guidance
- security reporting

Keep out of public docs unless explicitly intended:

- fundraising
- accelerator/VC narrative
- enterprise sales strategy
- pricing strategy
- competitor analysis
- private roadmap
- internal validation plans

Private strategy belongs in ignored local notes under `research/`.

## 8. Release-gate decision

A public-alpha recommendation is appropriate only when all of the following hold at the release commit. The final decision is the Phase 4 gate (rename + PyPI publish land there); ticked items still get a fresh look at that gate.

- [x] README doorway is clear and current. — Phase 3 README rewrite.
- [x] `agent-sentinel demo` works from a clean local clone. — Phase 1 (observed pre-rename); re-verify at the gate as `agentacct demo`.
- [x] `agentacct doctor` gives actionable next steps. — Phase 3 friendly first-run errors (empty-store and unknown-run-id hints, zero-scan explanation, neutral forwarding nudge).
- [x] Tests pass locally and in GitHub Actions. — `.github/workflows/tests.yml` + README badge; re-verify at the gate.
- [x] Security policy and contribution guidance exist. — SECURITY.md, CONTRIBUTING.md at the repo root.
- [x] Provider/integration claims are accurate and caveated. — sections 5–6 above (Phase 3 sweep).
- [x] Claude Code and Codex live smoke tests have passed or the README clearly labels them as pending. — last pass captured by the maintainer smoke gate; paid rerun at the gate.
- [x] Public docs contain no private strategy or secret material. — section 7 scan (Phase 3 sweep); re-verify at the gate.
- [x] Known limitations are explicit. — README "What it does not claim yet".
- [ ] Final public product name, public repository identity, and distribution channel are explicitly chosen and published. Until then the private checkout is the only release authority and no public install URL is advertised.
- [ ] Final public-alpha recommendation recorded by the maintainer at the Phase 4 gate.

If any item is not true, keep the repository private or label the gap clearly before sharing more broadly.
