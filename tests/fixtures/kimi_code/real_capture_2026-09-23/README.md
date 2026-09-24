# Kimi Code real-capture fixture — 2026-09-23

## Provenance

Captured from a controlled, minimal Kimi Code run on this machine on 2026-09-23:
one `printf` probe command, two LLM requests. The run's own agent id was
`agent-41` and the model Kimi Code assigned to that run was
`DeepSeek/deepseek-flash`. The source artifact was that run's own
`agents/agent-41/wire.jsonl` under the local `~/.kimi-code` store.

`agents/agent-41/wire.jsonl` in this fixture contains the **two `usage.record`
lines byte-verbatim** from that run: same field order, same values, same
millisecond timestamps. Nothing else was copied — every content-bearing event
type (`llm.request`, `context.*`, `agent.message.appended`, `metadata`, …) was
dropped, so this fixture is metadata-only by construction. It is the evidence
behind the `real_fixture` verification level cited by
`docs/adapter-capability-evidence.md`.

## Sanitized / reconstructed container (deviations from the raw store)

- Session id, workspace directory name, `cwd`, agent `homedir` values and
  `title` are neutral placeholders; `lastPrompt` is replaced by a redaction
  marker.
- `state.json` `createdAt`/`updatedAt` are the capture's real request
  timestamps (millisecond epoch), so the importer's second-resolution
  `started_at` matches the captured run.
- `session_index.jsonl` carries the real line **shape** with `sessionDir`
  tokenized as `<SESSION_DIR>`. Tests copy the fixture into a temp home and
  substitute the real path: the importer rejects index paths outside its home,
  so a literal absolute path committed to the repo would be wrong. The
  `sessions/<workspace>/<session_id>/state.json` tree is also complete, so the
  tree-scan fallback exercises the same data.
- `titleKind` and `isCustomTitle` mirror the values the local store writes.

## Expected import (sum of the two verbatim rows)

- model `DeepSeek/deepseek-flash`
- input 596, output 190, cache_read 44,800, cache_write 0
- 2 usage rows / 2 turns, cost unknown (Kimi Code persists no cost figure)

Captured by the agentacct maintainer session on 2026-09-23 while adding
Kimi Code support (work item: kimi-code usage-import evidence).
