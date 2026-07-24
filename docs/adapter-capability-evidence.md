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

- Claude Code and Codex MCP semantic verification is recorded separately in [Live MCP Client Smoke Results](live-mcp-client-smoke-results.md).
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
