# Data quality: what agents feed the UI, and what to enforce

Status: **analysis, no product code changed.** Date: 2026-09-12.
Worktree: `/Users/frank/Documents/agentacct-dataquality`, branch `data-quality-rules`,
based on `adab49f` (same baseline as the canonical checkout).

Scope: information extraction, the MCP recording surface, and how different
coding agents feed the ledger that the native app, TUI and dashboard render.

Evidence in this document is one of three kinds, labelled per claim:

- **Measured** — produced by `tools/audit-agent-data.py` against the installed
  store (`~/.local/state/agentacct/state/events.sqlite3`, 8,369 events) or by the
  write-path probe described in "What the write path already enforces".
- **Read** — verified by reading the cited source.
- **Inferred** — a judgement that follows from the two above but was not directly
  measured.

Re-run the audit:

```sh
# from this worktree; aggregate output only, safe to paste
# (.venv/bin/python, not a bare python3 — see REVIEW_GUIDE.md)
PYTHONPATH=src .venv/bin/python design-plans/data-quality/tools/audit-agent-data.py --limit 9000
```

## Headline: raw hygiene is good; structure is what is missing

The intuitive assumption — that different agents are feeding dirty, malformed
data — does **not** hold on the measured store. What is missing is structure:
required evidence, resolved attribution, and a work-level unit that the UI can
draw.

| Dimension | Measured value | Read |
| --- | --- | --- |
| Malformed records | 0 of 8,367 lines unparseable | clean |
| Checks pointing at a non-existent section | 0 of 335 | clean |
| Absolute or escaping paths in `files` | 0 of 650 entries | clean |
| Summaries truncated at the 1,200-character cap | 0 of 815 | clean |
| Sections stuck open (no terminal status) | 2 of 534 work items | clean |
| Titles under 8 characters / with control characters / with markup | 0 / 0 / 0 | clean |
| **Completed work items without strong machine-check evidence** | **405 of 530 (76.4%)** | the dominant gap |
| **Evidence-backed completion rate** | **21.7%** | reported by the ledger itself |
| **Work items joined to usage** | **208 of 534 (39%)** | 308 ambiguous, 16 missing keys, 2 unallocated |
| **Imported usage rows with no work context** | **2,087 (91.3% of usage)** | turn-granular facts, no work-level identity |
| Machine checks with no `artifact_ref`/`artifact_path`/`artifact_url` | 208 of 335 | no durable artifact pointer |
| Machine checks with no `files` | 272 of 335 | cannot say what was covered |
| Machine checks with no `command` | 64 of 335 | cannot be re-run |

## Two structural explanations

1. **Granularity mismatch.** Imported usage is recorded per model turn (2,402
   `model_usage` events), while agent-recorded meaning is per work item (534).
   The ledger states this itself: the join reason for **308** items is "Usage
   matches multiple MCP sections in the same client session; agentacct does not
   allocate section-level usage." Every turn therefore becomes its own row and
   its own attention signal, and 2,194 of 3,904 projected timeline rows carry no
   `work_id` at all.
2. **Attention inflation.** The projection raises **2,921 attention items**
   against 534 work items: 2,087 "usage row has no work context", 405
   high-severity "completed without strong evidence", 308 ambiguous, 105
   evidenced-but-unattributed, 16 missing `client_session_id`. Only 405 (14%) are
   the high-severity signal a person can act on; the rest is per-turn bookkeeping
   at the same visual weight. **Inferred:** this is a direct cause of the UI
   reading as noisy and of explanatory copy being needed to make sense of it.

**Inferred consequence for the timeline request:** the canvas is not crowded
because agents write long prose — measured titles are 40 characters at the median
and summaries 207. It is crowded because imported turn-level records are drawn as
if they were work items.

## What the write path already enforces

Probed directly through `SentinelMCPServer` in a temporary store (probe file was
removed; the matrix is reproducible from the transcript of this session).

Strict and loud today (**Read** + probe):

- `section_status` vocabulary. `in_progress`, `done`, `finished`, `STARTED `,
  `Completed` are all rejected with the exact allowed set; only the five canonical
  values pass.
- `files` path rules. `..` segments rejected with
  "must not escape the project directory"; absolute paths rejected unless
  `project_dir` is supplied on the same call, with a message naming both fixes.
- Title length. 500 characters rejected as
  "section_title must be <= 160 characters (received 500)" — limit *and* received
  size, the shape `_limit_error` documents at `src/agentacct/mcp.py:339`.
- Mangled tool calls. Detected and warned, never rejected or repaired
  (`src/agentacct/mcp.py:495-564`), calibrated against the real ledger with 2 true
  positives and 0 false positives.
- Unknown MCP arguments. `additionalProperties: false` plus
  `_reject_unknown_keys` (`src/agentacct/mcp.py:333`).
- Identity scope. `WorkEvent` is an allowlist that cannot carry prompts,
  transcripts, tool arguments or results (`src/agentacct/work_events.py:139-209`).

Accepted today that a display surface then has to cope with:

- A 3-space title (`"   "`) on a **completed** section. Non-empty is not the same
  as meaningful.
- A **completed** section with no summary at all. Measured: 5 of 536 terminal
  sections in the real store, 0.9%.
- `section_id` reused for two unrelated tasks: the second silently overwrote the
  first item's title in the projection.
- A machine check with no result detail: recorded summary is the synthesized
  `"pytest: passed"`, with no command, files or artifact.
- Re-using one `idempotency_key` with **different** content: the second call
  silently returned the first event and the new content was dropped, with no
  warning that a distinct report was lost.
- Unknown `metadata` keys were accepted and removed without comment.

## What the UI does with the fields (audited)

A separate pass mapped every recorded field to the surface that renders it. The
findings that changed the rule design:

- **The canvas card shows only title, lane and result** — not `summary`, `source`,
  files or scope (`WorkTimeCanvas.swift:182-198`). A 40-character title with a
  same-session sibling is genuinely ambiguous on the card; the distinguishing
  scope is one disclosure down (`WorkTimelineView.swift:638`).
- **An untitled section renders its raw `section_id` as the headline**
  (`work_ledger.py:4031` → `task_timeline.py:134` → `WorkTimeCanvas.swift:195`).
  The anti-id guard that exists for Task titles (`api.py:1064-1072`) does not
  cover this path — which is why R1 is a refusal, not a normalization.
- **A missing summary is not stated, it is implied**: the card reads
  "Reported completed" and nothing else (`WorkTimelineModel.swift:56-59`), and
  the same emptiness can surface a raw id as a Task "objective"
  (`receipt.py:760` → `ReceiptsPane.swift:976`).
- **`run_id` is dropped by every UI**; a check with no `section_id` and no run
  hint gets no section link, so its Checks block silently vanishes
  (`WorkTimelineView.swift:583`).
- **Absence and deliberate redaction look identical.** `command` is never
  stored; only a `command_redacted` flag is (`work_ledger.py:4120-4121`), so
  "deliberately not captured" (`WorkTimelineView.swift:648`) appears only when
  the agent *did* pass a command. An absolute `artifact_path` becomes `None`
  plus `artifact_path_redacted: true`, presenting a shape rejection as a privacy
  choice (`work_ledger.py:4124-4126`).
- **Placeholder copy is already load-bearing.** Invented strings compensate for
  empty fields in at least twenty places (`"Recorded work"`, `"Recorded check"`,
  `"Unnamed check"`, `"intentionally not captured"`, `"Source time unavailable"`
  …). Every rule that makes a field mandatory removes a placeholder rather than
  adding one, which is the direction the user asked for.

**Inferred:** the placeholder copy is a symptom of the same root cause as the
attention inflation — the surfaces are written to stay honest when the data is
missing, because the data is allowed to arrive missing.

## Three defects that are not about formatting at all

Checked while auditing extraction; each is a hole in the store, not a messy value.

### 1. Claude Code transcripts over the identity scan budget are dropped forever

`client_usage.py:92-93` caps the identity scan at 256 KiB / 256 lines;
`_peek_claude_session_id` (`:3103-3165`) returns "unknown" when the budget ends
first, the call records `claude_transcript_identity_scan_truncated`, the file is
excluded (`:2236-2276`), and the comment at `:2238-2241` explicitly refuses to
fall back to the filename stem. There is no repair or backfill path.

The live store shows the cost: health reports **972 consecutive failures**,
6,595 discovered files, 2,119 observed sessions parsed, and only **20 root
groups** returned against a scan limit of 20. Measured asymmetry: 2,119 observed
sessions but 1,996 usage sessions — 123 observed sessions have no usage row.

**Inferred:** this is why the ledger can be structurally incomplete while every
other hygiene metric is clean, and it is the one place where "the data is fine,
the import gave up" is literally true.

### 2. Codex usage rows carry no revision watermark, so a conflict can never clear

Measured `source_revision_at` coverage by source across all 2,402 `model_usage`
rows:

| source | rows | `source_revision_at` | `source_revision_basis` |
| --- | --- | --- | --- |
| claude-code-local-session-import | 2,005 | 2,005 | 2,005 |
| **codex-local-session-import** | **372** | **0** | **0** |
| opencode-local-session-import | 21 | 21 | 21 |
| **openclaw-local-session-import** | **4** | **0** | **0** |

`ClientUsageEvent` for codex sets neither field (`client_usage.py:1384-1520`;
the metadata builder writes them only when non-`None`, `:539-541`), so the
refreshable lane falls back to whole-second `threads.updated_at` (`:1421`). The
code already documents the hazard at `:337-351` — "whole-second client clocks
cannot order two real revisions inside one displayed second". Equal order plus a
different content hash is not provenance-only drift, so it parks as a permanent
conflict (`evidence_store.py:968-991`, `:1092-1114`), de-duplicated to one stable
row (`:846-861`), and **no reconcile path exists** for refreshable-usage
conflicts (`:1161-1169` only ever appends). Health then projects that one global
fact onto every configured source (`ingestion_health.py:359-410`).

**Fix direction:** give codex and openclaw usage rows a per-session microsecond
watermark like claude (`:2415`) and opencode (`:3837`) already have. Do not relax
the conflict logic. The Claude fix needs more care than a bigger constant:
`_peek_claude_session_id` (`client_usage.py:3103-3165`) reads a bounded *prefix*,
and Claude writes `sessionId` on the session's first records, so lifting the cap
re-reads more of every transcript on every refresh. The two candidate designs are
(a) cache the resolved identity keyed by (size, mtime) so the scan happens once
per file revision, or (b) resolve identity from the transcript's *first record*
only, which is where the field is written, instead of scanning until it is found.
Neither is implemented here; both need a re-import measurement.

### 3. Three parts of the contract reach no agent at all

- The **`files` path rule** — called "the single biggest MCP rejection cause" at
  `mcp.py:51-52` — exists only in schema descriptions (`mcp.py:53-59`), absent
  from the rendered `CLAUDE.md`/`AGENTS.md` block, the Hermes skill, and
  `docs/agentacct-workflow-instructions.md`.
- **`idempotency_key`** has no agent-facing guidance anywhere; it appears only as
  a schema property (`mcp.py:134,196,252`).
- **`artifact_ref` / `artifact_path` / `artifact_url`** are described nowhere,
  accepted at write (`mcp.py:1275-1277`), and their path/url are silently
  redacted at read (`work_ledger.py:4123-4127`, `4790-4804`).

The mechanism to fix all three exists: `_RECORDING_CONTRACT_LINES`
(`src/agentacct/install_guide.py:282-293`) is embedded verbatim by every
recording surface — MCP `initialize` instructions (`:324`), SessionStart context
(`:351`), and the managed `CLAUDE.md`/`AGENTS.md` block (`:429`) — and
`docs/agentacct-workflow-instructions.md` mirrors it verbatim today (verified by
diff). Editing that one constant is how a new rule reaches agents on
already-onboarded machines, which is what matters because those rendered files
are written once at onboard and never refreshed. Neither `INSTALL.md` (0
occurrences of `section_status`) nor the Hermes skill mirrors the contract.

## What this implies for rules

The measured evidence says the highest-value rules are not "reject dirtier
input". They are:

1. Make the evidence requirement structural, not advisory: a terminal
   `completed` section that names no check is the 405-item problem.
2. Resolve the actor automatically. 16 items are missing `client_session_id`,
   and the guidance already says never to guess — the fix is server-side capture
   (hook or attach), not a stricter schema.
3. Give usage a work-level home so 2,087 rows stop being individual signals.
4. Add the cheap display guarantees the probe shows are missing: a title that is
   really text, a stable `section_id`, an idempotency conflict that is reported
   rather than swallowed.

Each rule needs an enforcement point, a false-positive budget, and a measured
justification, in the style `mcp.py` already uses for the files rule and the
mangle detector. That catalogue is in [RULES.md](RULES.md).


## Resolution status — 2026-09-12

Everything above was addressed in this branch. Measured outcomes, not intentions:

| Defect | Before | After |
| --- | --- | --- |
| Claude transcript identity unresolved | 5.13% of 6,595 files | **0 of 6,274 real transcripts** (321 workflow journals correctly excluded) |
| Codex usage rows with a revision watermark | 0 of 372 | every row, from the rollout file's microsecond mtime |
| Rules enforced per lane | MCP handler only; HTTP and CLI could store what MCP refused | one implementation, enforced once at `SentinelService.record_event` |
| Task rows the UI cannot name | 4 of 55 Tasks read "Untitled OpenClaw chat" | distinct per session and day, e.g. "OpenClaw session · 12 Sep" |

The Claude fix is the budget, not the mechanism: the earlier 256 KiB / 256-line
peek resolved 94.87% of files, and the 41 that failed were the genuinely large
ones. A normal transcript carries `sessionId` at roughly byte 4,000 on its FIRST
line, so raising the budget to 2 MiB / 512 lines recovers 17 of those 41 and
costs no measurable time -- 0.8 s to scan all 6,595 files either way, because the
scan stops at the first match. The remaining 320 files contain no `sessionId` at
all: they are `journal.jsonl`, which the importer already excludes before the
identity pass (`client_usage.py:2154`), so the health signal no longer counts
them as failures.

One behaviour was left as it is, deliberately: a file whose identity scan
COMPLETES without finding an id still falls back to `path.stem`
(`client_usage.py:2268`). That is the documented second rule the audit flagged as
inconsistent, but changing it would alter identity assignment for files that
currently import, and no measurement yet shows it producing a wrong root. It is
recorded here as a known open question rather than silently changed.
