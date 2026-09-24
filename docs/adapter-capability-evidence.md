# Adapter capability evidence

This page records bounded evidence referenced by the machine-readable agent capability manifest. A result verifies only the lane named here; it does not turn an agent into a binary “supported” client.

## 2026-07-17 local import and Dashboard observation

The post-P5 private-build checkpoint ran a real local import against the maintainer's existing client stores, then restarted the ownership-safe local Dashboard and inspected the resulting API and UI.

Observed import result:

- 1,414 client sessions observed.
- 1,409 sessions had client-reported usage.
- 5 real sessions had no usage row and remained visible as observation-only sessions.
- Claude Code: 699 observed sessions, 695 usage sessions, 4 without usage, no current ingestion errors.
- Codex: 682 observed sessions, 681 usage sessions, 1 without usage, no current ingestion errors.
- Hermes: 33 usage-bearing sessions remained present after refresh.

Observed product result:

- The Dashboard health endpoint returned HTTP 200 after an ownership-safe restart.
- The ingestion health endpoint reported healthy with no current issues.
- An observation-only Task rendered token, cache, and cost fields as `Unavailable`; it did not manufacture zero usage or zero cost.
- The usage summary kept 651 non-additive Codex replay-like rows out of additive totals while preserving them for inspection.

This is a live observation of the declared local import paths on one machine, not a cross-version stability guarantee. The manifest therefore uses `verified_partial`, not a whole-client stable or supported claim. Hermes remains explicitly limited because only usage-bearing rows have live-store evidence; zero-token observation and multi-home fail-closed behavior are deterministic-fixture verified, while schema-drift recovery remains provisional.

## Evidence boundaries

- Hermes, OpenCode, and OpenClaw MCP verification is recorded separately in [Coding agent integrations](coding-agent-integrations.md#maintainer-real-client-smoke-results).
- Synthetic parser fixtures prove deterministic field handling only. They do not prove current client schema compatibility, installation, or runtime health.
- Runtime presence belongs to `/usage/sources`; latest importer health belongs to `/ingestion/health`; historical evidence belongs to `/evidence/product`.

## 2026-07-17 Cursor 3.9.16 primary-state observation

A read-only dry run exercised the primary Cursor 3.9.16 store on the maintainer machine while Cursor had an empty regular WAL sidecar and a regular 32 KB SHM sidecar.

Observed result:

- 35 `composerData:*` sessions discovered.
- 20 recent root groups selected and 20 session observations returned.
- 0 usage events and 20 sessions with usage unavailable.
- 5 explicit model labels retained; missing/default labels remained unattributed.
- 0 titles and 0 working directories retained.
- 0 scan errors.
- An isolated-store write persisted 20 `session_observed` events carrying no usage, token, cache, or cost fields.
- Repeating the import wrote 0 new events and preserved the same 20 observations, proving the bounded path is idempotent.

This verifies the bounded primary-state session-discovery and explicit-model lanes on one Cursor version. It does not verify token, cache, or cost import, and it is not a multi-version stability claim.

## 2026-07-17 Cursor observation-only fixture boundary

Deterministic fixtures exercise the primary `cursorDiskKV` composer path: metadata-only root/child observation, root-group limiting, source namespaces, id/schema/graph validation, symlink rejection, active-WAL rejection, corrupt/replaced database handling, and source-discovery/import parity. The fixtures also prove that usage, cache, cost, prompt/message/title, cwd, and project data do not enter the produced observation.

Fixtures remain the evidence for malformed/schema/path/lineage failure handling. The separate Cursor 3.9.16 smoke above verifies the positive session-discovery and explicit-model lanes on one real version, so the manifest marks only those bounded capabilities `verified_partial` with `single_machine_live_observation` stability. Usage, cache, cost, automatic installation, and multi-version stability remain unavailable or unverified as declared in the manifest.

## 2026-09-16 DeepSeek Harness (dsh) 0.1.5-rc.1 MCP self-reporting smoke

A real dsh 0.1.5-rc.1 headless session, configured only by `agentacct onboard --agent dsh` (the home patch `$DSH_HOME/cordis.patch.yml` plus `$DSH_HOME/AGENTS.md`), loaded the bundled `@deepseek-ai/dsh-mcp-client` plugin in-box — no `dsh plugin --profile <name> add` step was needed — launched agentacct's MCP server, and called `agentacct_record_section` (started, then completed). The isolated agentacct store recorded both events with `source: dsh` and the requested section id.

This verifies the positive MCP self-reporting lane end to end — home-patch plugin resolution, server launch, tool availability, and the record pipeline — on one real version and one machine, with the recording task explicitly requested. It is not a cross-version stability guarantee and does not verify spontaneous (uninstructed) recording. The manifest therefore marks the dsh `mcp_semantics` and `automatic_install` lanes `verified_partial` with `single_machine_live_observation` stability. The usage-import lanes (session-log parsing) remain synthetic-fixture `experimental`: dsh writes Zstandard-compressed JSONL session logs whose parsing is verified against source and third-party parsers only, with no live-client log smoke yet.

## 2026-09-23 Kimi Code usage-import fixture boundary

Deterministic fixtures exercise the Kimi Code local usage-import path:

- `session_index.jsonl` session discovery and bounded session selection.
- Per-agent `wire.jsonl` `usage.record` parsing and field mapping (`inputOther` → input, `output` → output, `inputCacheRead` → cache read, `inputCacheCreation` → cache write).
- Aggregation of the per-request increments (the client reports deltas, never cumulative totals) into session totals.
- Malformed/corrupt-source diagnostics and the session-limit boundary.

The named evidence tests are `tests/test_client_usage.py::test_discover_kimi_code_usage_reads_wire_jsonl_usage_records`, `tests/test_client_usage.py::test_discover_kimi_code_usage_reports_diagnostics_on_corrupt_source`, and `tests/test_client_usage.py::test_discover_kimi_code_usage_respects_session_limit`. Fixtures prove deterministic field handling only; they also prove the import stores no prompt, message, or transcript content.

These fixtures stay the evidence for malformed-input diagnostics, the dedupe/crash-append handling, and the session-limit boundary. The real capture and the same-day live smoke recorded in the next section supersede the earlier `experimental` ratings for `session_discovery`, `usage_import`, `model_attribution`, and `cache_read`; `cache_write`, zero-usage observation, and namespace hardening stay unclaimed, and `mechanical_capture`, `mcp_semantics`, and `automatic_install` remain unavailable (MCP registration is a manual `~/.kimi-code/mcp.json` preview with no agentacct writer). Cost is never client-reported: `usage.record` rows expose token/cache fields but no provider-billed cost, so the only figure available is a pricing-table estimate. Since the same-day pricing-resolution fix, `--estimate-costs` resolves the observed ids (`kimi-code/k3-256k` through the Moonshot K3 row, `DeepSeek/deepseek-flash` through the DeepSeek Flash row) and leaves ids the local table does not cover at `unknown`.

## 2026-09-23 Kimi Code real-capture fixture and live smoke

Two real, dated artifacts from the maintainer machine back the kimi-code import lanes: a committed capture of one controlled run, and a same-day live comparison against the machine's own Kimi Code store.

**Capture.** A controlled, minimal Kimi Code run — one `printf` probe command and two LLM requests — whose own agent id was `agent-41` and whose assigned model was `DeepSeek/deepseek-flash`. `tests/fixtures/kimi_code/real_capture_2026-09-23/` holds that run's two `usage.record` lines byte-verbatim from `agents/agent-41/wire.jsonl` (same field order, same values, same millisecond timestamps) and drops every content-bearing event type (`llm.request`, `context.*`, `agent.message.appended`, metadata), so the artifact is metadata-only by construction. The surrounding container is reconstructed and sanitized:

- Session id, workspace directory name, `cwd`, agent `homedir` values, and `title` are neutral placeholders; `lastPrompt` is a redaction marker.
- `session_index.jsonl` keeps the real line shape with `sessionDir` tokenized as `<SESSION_DIR>`: the importer rejects index paths outside its home, so a literal absolute path committed to the repo would be wrong. Tests copy the fixture into a temp home and substitute the real path.
- `state.json` `createdAt`/`updatedAt` are the capture's real request timestamps (millisecond epoch), so the importer's second-resolution `started_at` matches the captured run, and the complete `sessions/<workspace>/<session id>/` tree also exercises the tree-scan fallback.

Expected import, which is the sum of the two verbatim rows: model `DeepSeek/deepseek-flash`, input 596, output 190, cache read 44,800, cache write 0, two usage rows / two turns, and no client cost figure (Kimi Code persists none; a `--estimate-costs` import prices this row from the DeepSeek Flash list price as a ≈ estimate). The named evidence test is `tests/test_client_usage.py::test_discover_kimi_code_usage_reads_real_captured_wire_fixture`.

**Live smoke.** Also on 2026-09-23, at 17:50:35 local, a read-only import ran against the maintainer's real `~/.kimi-code` store and was compared with an independent script that sums the wire files directly. All six session×model lanes agreed exactly — each of the three live sessions carried two client-reported model lanes (`DeepSeek/deepseek-flash` and `kimi-code/k3-256k`) — and every lane reported cache write 0. The compared streams were checked for new `usage.record` rows before and after the comparison (quiet checks passed), so both sides summed the same input. Per-lane token totals are deliberately not published here.

That dated capture plus the same-day single-machine live comparison is what the manifest's `real_fixture` level cites; no Kimi Code client version string was recorded, so `client_versions` stays empty. The manifest marks the kimi-code `session_discovery`, `usage_import`, `model_attribution`, and `cache_read` lanes `verified_partial` with `single_machine_live_observation` stability, and the honesty boundaries are:

- **Single machine, single client build.** The capture and the live comparison both come from one machine; multi-version, multi-machine, and long-run stability are not claimed.
- **No real cache-write evidence.** `inputCacheCreation` was 0 in both captured rows and in every live lane, so the non-zero path has never been observed: `cache_write` stays `experimental` on `synthetic_fixture` verification rather than being upgraded on a path no real data exercised.
- **Happy path only in the live evidence.** Malformed-input diagnostics, dedupe of crash-double-appended rows, and the session limit remain synthetic-fixture evidence, as does zero-token session observation (no usage-row-less session was in the capture or the live store), and namespace hardening is not claimed.
- **Not billing.** Tokens are client-reported and no client cost figure exists; a `--estimate-costs` run resolves the observed ids (K3, DeepSeek Flash) to pricing-table ≈ estimates and leaves uncovered ids `unknown`. The number is always an equivalent-cost estimate, never provider billing.
