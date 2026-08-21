# Multi-source evidence privacy threat model

Status: implementation baseline
Last updated: 2026-07-13

## Security objective

agentacct should explain what work happened and why the evidence supports
that conclusion without becoming a second transcript archive. Metadata-only,
local-first collection is the default. Content collection is a separate,
explicitly consented capability and is outside the first multi-source release.

## Protected data

The highest-risk data is:

- prompts, assistant responses, and hidden reasoning;
- tool arguments and results;
- source file contents and patches;
- shell commands containing secrets;
- transcript/rollout paths and raw logs;
- provider API keys, cookies, and auth headers;
- user names, absolute home paths, repository remotes, and issue text;
- billing account identifiers and invoice details.

The first release needs none of the content categories above to provide its core
evidence views.

## Trust zones

1. **Host agent**: emits hooks and owns private transcript/rollout files.
2. **Local adapter**: parses bounded input and removes non-allowlisted fields.
3. **Raw spool**: local append-only metadata evidence, not raw host payloads.
4. **Projection/API**: derived, queryable views with additional path redaction.
5. **Controller**: may act on a signal and therefore requires a stronger basis
   than an ordinary display claim.

Provider and orchestrator records are trusted only for declared dimensions, not
as arbitrary input to agentacct's process or filesystem boundary.

## Default capture profile

The profile name is `metadata_only_v1`.

Allowed examples:

- source system/type and adapter/schema versions;
- stable opaque session, turn, tool, trace, span, task, run, and commit IDs;
- timestamps, duration, exit status, and bounded counters;
- tool category and machine-check category;
- repository-relative normalized path and content digest;
- model/provider identifiers and numeric usage/cost with explicit basis;
- lifecycle status, completeness, truncation, and redaction flags;
- user/agent semantic summaries only when explicitly sent through a Work Event.

Denied by default:

- prompt, response, thought, transcript, tool input, or tool output;
- source code, patch body, command stdout/stderr, or environment values;
- authorization, cookie, secret, and token fields;
- absolute paths when a repository-relative or basename representation exists.

Unknown fields are denied. An upstream schema update cannot expand capture by
accident. Explicit restricted raw-content capture can admit a prompt/body when
the caller opts in, but it never admits credential, header, cookie, secret, or
`*_token` keys; that invariant is rechecked during deserialization and replay.

## Threats and mitigations

| Threat | Consequence | Required mitigation |
| --- | --- | --- |
| Hook payload contains prompt/tool content | Private content persists locally or appears in UI | Per-adapter allowlist, deny unknown fields, privacy snapshot tests |
| Malicious event claims trusted provenance | Fake provider, CI, hook, or usage truth | Source policy and transport-specific constructors; generic writers cannot mint trusted source types |
| Duplicate/out-of-order events | Double-counted usage/cost or false completion | Deterministic idempotency, append-only conflicts, ordered projections |
| Source overwrites earlier evidence | Audit history is lost | Immutable envelope; superseding evidence is a new envelope |
| Cross-session identifier collision | Work and usage attributed to wrong task | Canonically encoded issuer/organization/project/source-instance namespace, content-addressed graph nodes, conflict veto, no false exact |
| Prompt text smuggled into a nominal ID | Content leak through metadata field | length/character bounds, digest opaque values, reject multiline IDs |
| Absolute local paths exposed in API | User/workspace identity leak | path redaction and repository-relative projection |
| Raw spool is world-readable | Local user data disclosure | create directories/files with user-only permissions where supported |
| Spool/index divergence after crash | Missing or inconsistent evidence | append then transactional index; replay from spool; integrity hashes |
| Controller acts on stale/conflicting signal | Wrong run paused/cancelled | advisory default; expiry; supporting evidence; conflict and ownership checks |
| MCP self-report treated as objective | False completion/cost claim | `claim` classification; separate machine/provider evidence |
| Hook slows or blocks host agent | Developer workflow outage | bounded input; local append only; fail-open; no network/dashboard rebuild |

## Data minimization invariants

- Store normalized evidence, not the original hook response.
- `raw_ref` points only to a deliberately retained local object; it is absent in
  metadata-only mode.
- `raw_digest` proves input identity without retaining its content.
- Relative path lists are bounded and may be replaced by digests when large.
- Semantic summaries are allowed only through explicit Work Event fields; they
  are never synthesized from prompt/response content.

## Retention and deletion

Evidence v2 uses a dedicated store so retention does not mutate v1 history.
Future retention policies may compact low-value activity after preserving a
summary and integrity manifest. Provider-billed records, explicit user Work
Events, objective machine checks, and legal/audit holds require separate policy.

Deleting a projection is safe because it can be rebuilt. Deleting immutable raw
evidence is an explicit retention action and must never be disguised as replay,
dedupe, or rollback.

## Network and configuration boundaries

- Hook capture performs no network calls.
- No installer changes global Claude Code, Codex, Cursor, or Git config
  without a separate explicit user action.
- No provider key is requested or read for evidence ingestion.

## Verification gates

Release tests must prove:

1. canary prompt/response/thought/tool-body strings never appear in spool,
   SQLite projection, API JSON, or rendered HTML;
2. auth-like attribute names and multiline/oversized identifiers are rejected or
   redacted;
3. a duplicate payload is idempotent;
4. a conflicting payload is preserved and surfaces a discrepancy;
5. hooks return an allow/fail-open result when agentacct capture fails;
6. a control signal without an eligible basis cannot become hard enforcement.

## Content-mode future work

If a later product intentionally captures transcripts or tool bodies, it needs a
separate RFC covering informed consent, field-level encryption, key ownership,
retention, export/delete behavior, multi-user authorization, and hosted-service
boundaries. It must not be introduced by widening `metadata_only_v1`.
