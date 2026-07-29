# Usage and cost truth table

Agent Chronicle keeps integration evidence separate from billing claims. This page explains what each path can prove today.

Use the CLI version:

```bash
agent-chronicle usage truth-table
agent-chronicle usage truth-table --json
```

## Summary

| Integration path | What Chronicle can observe | Update timing / freshness | Usage confidence | Cost confidence | Hard budget basis |
| --- | --- | --- | --- | --- | --- |
| Codex local usage import | Local Codex sessions, exact recorded parent lineage, model when present, non-cached input, cached input, output, reasoning tokens, turn count, timestamps | Repeated rollout `token_count` events during a thread; `--limit-sessions` selects complete recent root groups, and later token growth reaches Chronicle only via dashboard refresh or an importer run with `--refresh` | `client_reported` | `unknown`; optional `estimated_from_tokens` with `--estimate-costs` | Advisory only from import; use token/runtime limits or proxy for hard dollar stops |
| Claude Code local usage import | Local Claude Code project JSONL usage, model when present, input, cache creation/read, output tokens, turn count | Assistant message rows carry `message.usage` as transcript files are written; local `journal/result` rows are not the usage source in observed samples | `client_reported` | `unknown`; optional `estimated_from_tokens` with `--estimate-costs` | Advisory only from import; use token/runtime limits or proxy for hard dollar stops |
| OpenCode JSON event stream import | OpenCode JSON/JSONL `step-finish` token fields and client-reported cost when present | Usage/cost appear on captured `step-finish` events | `client_reported` | `client_reported` | Useful for dashboards/advisory budgets, not provider invoice truth |
| Hermes local state import | Hermes `state.db` session rows, provider/model, input/cache/output/reasoning tokens, message count, client-reported cost fields | Session-level fields are read from Hermes' local SQLite state database | `client_reported` | `client_reported` when Hermes stores cost fields | Useful for dashboards/advisory budgets, not provider invoice truth |
| OpenClaw JSONL local usage import | OpenClaw JSONL assistant usage rows, provider/model, input/cache/output tokens, optional client-reported cost fields | Usage appears on assistant message rows when OpenClaw writes JSONL session logs | `client_reported` | `client_reported` when `usage.cost.total` is present; otherwise `unknown` | Useful for dashboards/advisory budgets, not provider invoice truth |
| Cursor primary state observation | Composer identity, client timestamps, explicit `modelConfig.modelName`, and exact child links from primary `User/globalStorage/state.vscdb` only | On explicit import, Dashboard refresh, or watcher scan; active WAL and source/schema races fail closed | `unknown`; no token fields are selected | `unknown`; no cost fields are selected | No token/dollar enforcement; proves bounded session presence only |
| Agent MCP workflow events | Section `started`/`checkpoint`/`completed`/`blocked` events, machine checks, and blocker context reported by an MCP-capable agent | Only when the configured agent calls Chronicle MCP tools | `unknown` unless the agent supplies usage metadata | `unknown` unless the agent supplies cost metadata | Not a billing source by itself |
| Agent MCP usage debug snapshots | Agent-visible token/cost numbers or an explicit unavailable marker, plus client/session/turn join keys | Only when the configured agent calls `agentacct_record_agent_usage_debug`; stored as metadata for comparison | `unknown` in Chronicle totals; debug values live under `metadata.agent_reported_*` | `unknown` in Chronicle totals; debug cost lives under `metadata.agent_reported_cost_usd` when provided | Comparison evidence only; never a hard budget basis |
| Native coding-agent hook capture | Host-emitted lifecycle/session/turn/tool ids, relative file metadata, subagent links, and recognized machine-check exit codes | On each explicitly activated Claude Code, Codex, or Cursor hook | `unknown`; hooks do not capture tokens | `unknown`; hooks do not capture cost | No token/dollar enforcement; objective exit codes may be machine evidence |
| OpenLIT / OTLP trace ingestion | Trace/span/session ids, runtime lifecycle, tool-span metadata, allowlisted model labels, and telemetry-reported usage/cost fields when present | As spans reach local `POST /v1/traces`, or on explicit JSON import | `telemetry_reported`, corroborating unless measurement provenance proves more | `telemetry_reported`, never provider-billed merely because it used OTLP | Advisory unless separately backed by provider-billed evidence |
| Paperclip snapshot connector | Work-item/execution/agent/work-product and cost claims from an exported snapshot | On explicit snapshot import | `unknown` | `orchestrator_claim`; missing and zero remain different | Advisory; Chronicle never dispatches a Paperclip action |
| Entire Git checkpoint connector | Public checkpoint/commit/session ids and allowlisted change/usage metadata | On explicit read-only repository scan | `checkpoint_reported`, corroborating only | `unknown` | No hard budget basis; Git proves artifacts, not billing |
| Provider/API proxy | Provider/model, request estimate, provider usage fields, provider-billed cost when returned | Request estimates before forwarding; provider usage/cost after each proxied response | `provider_reported` when returned | `provider_billed` when returned; otherwise `estimated_from_tokens` | Strongest hard dollar enforcement path for traffic routed through Chronicle |
| Chronicle-owned process wrapper | Runtime, exit code, stdout/stderr logs, pause/resume/kill ownership metadata | Process state while Chronicle owns the command; token/cost freshness needs import, proxy, or MCP events | `unknown` | `unknown` | Useful for process/runtime control, not token or billing truth by itself |

## Important boundaries

- MCP setup proves that an agent can report work to Chronicle. It does not automatically parse private session logs or prove exact billing.
- MCP is a first-class semantic source, not a fallback to hooks. Hooks/OTLP can prove mechanical activity when MCP did not fire; they cannot invent task meaning.
- Telemetry means machine-emitted runtime evidence. OTLP is its transport format, not a provider invoice and not the MCP protocol.
- Metadata-only hooks do not capture token usage or cost. They deliberately omit prompts, responses, thoughts, tool arguments/results, stdout, and stderr.
- Paperclip rows are orchestrator claims. Entire rows prove Git/checkpoint artifact metadata. Neither is silently promoted to provider billing or objective completion.
- Agent usage debug snapshots prove only what the agent reported seeing about itself, including explicit "unavailable" reports. They are not included in Chronicle's token/cost totals.
- Local usage import proves Chronicle parsed an implemented local client session/export path. Imported usage is client-reported and sanitized; prompts/transcripts are not stored in Chronicle events.
- Current local usage truth rows include both `metadata.usage_source=local_client_session_store` and `metadata.usage_provenance=agent_sentinel_local_usage_import`. Ordinary event writes and MCP `agentacct_record_event` cannot create this trusted provenance.
- Pricing-table estimates are equivalent-cost estimates, not provider invoices.
- Provider/API proxy data applies only to traffic intentionally routed through Chronicle.
- Subscription tools such as Claude Code and Codex may expose useful per-session token data without exposing exact marginal subscription billing.
- Current local Codex and Claude Code subscription samples expose token/cache fields but do not expose provider-billed cost fields to Chronicle.
- Ingestion receipts prove what Chronicle attempted and parsed from each configured local source. They do not upgrade client-reported usage to provider billing or prove that a stopped watcher will capture future sessions.

## Pre-provenance ledger migration note

Older Agent Chronicle local usage import rows may have `metadata.usage_source=local_client_session_store` but no `metadata.usage_provenance`. Those rows are preserved as historical or diagnostic events, but they are no longer counted as usage truth totals.

To regenerate trusted usage rows, re-run:

```bash
agent-chronicle usage import-local --client all
```

or use the dashboard refresh action. The CLI importer recognizes legacy local-import-shaped rows for dedupe so an upgrade does not duplicate the same `client_session_id`; dashboard refresh can replace legacy rows for the same client/session with trusted rows. If you want a clean dogfood ledger, archive the old `events.jsonl`, clear the active event ledger, and then re-import local usage. Do not delete the source client logs under Codex, Claude Code, Hermes, OpenCode, or OpenClaw.

Old MCP semantic events from pre-MCP-v1 dogfooding can also be noisy. They can be archived with the old event ledger and left out of the active ledger when starting a clean local MCP v1 dogfood run.

## Observed local timing

Codex local sessions write repeated rollout `token_count` events with `last_token_usage` and `total_token_usage`. On the observed local subscription sample, these appeared throughout an active thread, not only in a final archived-session record.

Claude Code local project files write usage on assistant message rows. On the observed local subscription sample, `journal.jsonl` `started`/`result` rows did not carry token or cost usage; they describe task orchestration results rather than billing truth.

For dashboard UX, treat every local import as a snapshot. The CLI importer and `usage watch` record each session once at first observation and never update it by default — a background watcher does NOT keep an already-imported session's totals live. Only the dashboard's "Refresh & save usage" button, or an importer run with `--refresh`, replaces re-observed rows with fresh totals. Show both `last imported at` and source `last updated at` before implying that a number is live.

Local imports also preserve parent/child session metadata when the client exposes it. Claude Code subagent transcript files are linked back to their parent `sessionId`, and Codex child threads use `thread_spawn_edges` or the rollout's own `session_meta.parent_thread_id`. If Codex marks a thread as a subagent without exposing a parent edge, Chronicle records it as a child with an unknown parent instead of guessing a root from timestamps or working directory.

Codex applies `--limit-sessions` after resolving that exact lineage. The number is a count of recent root conversation groups; activity on a descendant makes its group recent, and every discovered row in a selected group is returned. A missing-parent orphan remains its own group. Per-source diagnostics make the scope explicit with `limit_unit=root_groups`, `selected_root_groups`, `returned_rows`, and `excluded_by_limit`.

Codex internal workflow labels such as `codex-auto-review` are not model names. An internal row with no directly reported model may inherit one only from its exact recorded parent session. The saved usage metadata records `client_model_source=inherited_exact_parent_session` and the parent id when that happens. Without that exact parent evidence, the model remains unknown and the row remains unpriced; Chronicle never applies a scan-wide model fallback.

## Ingestion receipts and sync health

A persisted import or dashboard refresh records owner-only, atomic per-source receipts at `<store-dir>/ingestion-health/state.json`. Receipts include `last_attempt_at`, `last_success_at`, parsed/skipped/error counts, a bounded error code, source watermark, scan limit, and source-specific limit diagnostics. `--dry-run --json` returns the same discovery diagnostics without writing usage rows or health state.

Inspect operational state through either interface:

```bash
agent-chronicle usage health
agent-chronicle usage health --json
curl http://127.0.0.1:8765/ingestion/health
curl http://127.0.0.1:8765/health
```

`GET /health` answers process/API liveness with `ok`; it also embeds `ingestion_status` and the ingestion snapshot. A green `ok` does not erase a degraded sync. `GET /ingestion/health` and `usage health --json` expose the direct per-source view. A clean manual scan leaves the overall state `unknown`, because it proves the past scan succeeded but not that future sessions are being watched. Overall `healthy` requires a fresh watcher heartbeat plus a successful receipt, from the current watcher epoch/build, for every source that watcher is configured to scan. Older receipts outside the live watcher's scope remain visible as historical rows but do not make the live watcher falsely green or red.

`usage watch` uses a single-writer lease per store and refreshes its heartbeat around scans. A live duplicate is rejected before scanning; a stale lease can be replaced, and a lost lease makes the former watcher exit instead of continuing as a competing writer. A watcher process keeps the code version loaded when it started, so upgrades require an explicit watcher restart. Stop it through the terminal or process supervisor that launched it, start the watch command again, then confirm the new `watcher.importer_version` and heartbeat with `usage health --json`.

Join health has a different scope from scan health. `agent-chronicle event summary --json`, `agentacct_get_event_summary`, and `GET /events/summary` calculate canonical context-match and attribution coverage over every matching store event, even when `limit` bounds the recent aggregate window. `result_scope.partial` explains that recent window; `usage_context_bridge.detail_scope.partial` explains any capped `links`, `attributions`, or `unlinked_contexts` arrays. The full-store ratios, `health_status`, `coverage_complete`, and `degraded_reasons` remain canonical and must not be recomputed from the returned detail sample. The older `joinable` field retains its compatibility meaning of instrumentation readiness; it is not a completeness score.

## Pricing catalog

Optionally estimate equivalent cost from known model pricing rows:

```bash
agent-chronicle usage import-local --client all --estimate-costs
```

The built-in pricing catalog is intentionally small. To extend coverage without
changing code, Chronicle keeps a local LiteLLM
`model_prices_and_context_window.json` snapshot in Chronicle state. Dashboard
and `usage import-local --estimate-costs` automatically use that state-local
snapshot when it exists:

```bash
agent-chronicle cost pricing-catalog --store-dir .agent-sentinel/state --refresh
agent-chronicle cost pricing-catalog --store-dir .agent-sentinel/state --provider openai --model gpt-4o-mini --json
agent-chronicle usage import-local --client all --estimate-costs
```

**Auto-refresh (TTL).** Every pricing path — `usage import-local
--estimate-costs`, each `usage watch --estimate-costs` tick, and the
dashboard's "Refresh & save usage" button — refreshes the store-local snapshot
automatically when its `fetched_at` sidecar timestamp is older than 7 days,
the snapshot is missing, or the snapshot no longer parses (a torn/corrupted
file counts as stale and is repaired; the sidecar records
`last_refresh_error: snapshot unreadable` until it is). A `fetched_at` more
than a few minutes in the future (clock skew at fetch time) also counts as
stale. The refresh is best-effort by contract: a failed download records
`last_refresh_error` / `last_refresh_attempt_at` in the sidecar, keeps the
stale snapshot, and never blocks or fails an import. Failed attempts back
off: while `last_refresh_attempt_at` is less than an hour old the refresh is
skipped entirely (reason `throttled`), so in the steady state an unreachable
or slow network costs at most one download attempt per hour per store —
never one per tick — and every import proceeds immediately on the existing
snapshot. Snapshot writes are atomic (per-write unique temp file + rename),
so a concurrently-serving dashboard never reads a torn file. Disable the
auto-refresh with `AGENT_CHRONICLE_PRICING_AUTO_REFRESH=0`; `cost
pricing-catalog --refresh` stays available as the force-now path. No
telemetry: the download is a plain GET of LiteLLM's public open-source
pricing table — nothing about your machine, store, or usage is sent.

You can still point at an explicit local Chronicle or LiteLLM JSON catalog with
`--catalog-path` or `AGENT_CHRONICLE_PRICING_CATALOG_PATH`; a pinned catalog is
respected as-is and is never auto-refreshed.

Provider aliases: `claude-code` rows fall back to `anthropic`-keyed prices and
`codex` rows to `openai`-keyed prices when no exact `(provider, model)` entry
exists — exact entries (including the deliberate builtin `codex`/`gpt-5.5`
fast-pricing rows) always win before the alias. The builtin fast-pricing rows
(the ones carrying a cost multiplier) are also protected on the merge path:
an external snapshot or pinned-file row keyed exactly `codex`/`gpt-5.5` never
replaces them, so the 2.5x convention cannot be silently dropped by upstream
catalog drift; external rows still extend coverage for every key the builtin
table lacks.

The pricing refresh downloads only the public LiteLLM model cost map. Estimated
costs are labeled `estimated_from_tokens`, not provider billing. Unknown models
remain unpriced until a trusted catalog entry exists.

**Stored-row stability vs. the unknown→priced transition.** Once a stored row
is priced, later catalog price drift never rewrites it. The ONE exception is
the unknown→priced transition: on a `--refresh --estimate-costs` scan (or the
dashboard refresh, which always prices), a re-observed row whose stored
`cost_confidence` is `unknown` and whose `(provider, model)` NOW resolves in
the catalog is replaced with a priced row — `pricing_source` provenance
stamped, event id reissued once — after which it is priced and stable like any
other row. `client_reported` costs are never overwritten.

## Practical interpretation

For developer onboarding:

```bash
agent-chronicle init --agent codex
agent-chronicle usage import-local --client all --dry-run --json
agent-chronicle usage discover-sources
agent-chronicle usage truth-table
agent-chronicle serve --store-dir .agent-sentinel/state
```

For API-key experiments with hard dollar caps:

```bash
agent-chronicle cost proxy \
  --enable-forwarding \
  --forward-provider openai \
  --max-total-usd 0.01
```

For normal subscription-based coding-agent workflows, treat local import and MCP events as a local activity ledger. Use the confidence labels on reports and dashboards before making budget or ROI claims.
