# Proposed rules for agent-fed data

Companion to [FINDINGS.md](FINDINGS.md). Every rule here is written to be
implementable and testable, and each carries the measurement that justifies it.
Rule style follows the two rules `mcp.py` already ships in this form: the `files`
path rule (`src/agentacct/mcp.py:53-59`) and the mangled-tool-call detector
(`src/agentacct/mcp.py:495-564`), both of which state why they exist and what
they refuse to do.

## Rule template

Each rule must declare five things, or it does not ship:

1. **Claim** — the exact condition, in one sentence.
2. **Why** — the measured ledger fact or the display defect it prevents.
3. **Enforcement point** — where it fires, per transport.
4. **Severity** — one of the four tiers below; severity decides whether a caller
   sees a refusal, a repair, or a warning.
5. **False-positive budget** — how many legitimate inputs the rule may reject,
   measured against the real ledger before merging. The mangle detector ships at
   "2 true positives, 0 false positives across 3,535 events"; that is the bar.

## Severity tiers

Tier is assigned by a single test: **can an agent fix this by re-sending the same
report with more detail?** If yes, refuse and say exactly what to add. The goal is
that an incomplete report essentially never reaches the store — every field the UI
needs is either supplied or the call comes back with instructions to supply it.
Accepting a report that the UI cannot render is what produces placeholder copy and
unreadable rows, so the system prefers one corrective round-trip to a permanently
poor record.

| Tier | Behavior | Use when |
| --- | --- | --- |
| **R — Refuse** | Reject, name the rule, and state the concrete fix. | The report is incomplete *and* an agent can supply the missing field in a retry. This is the default for UI-rendered fields. |
| **N — Normalize** | Accept, deterministically repair, record that a repair happened. | Exactly one honest repair exists and refusing would be pedantic (whitespace, control characters, timestamps). |
| **Q — Quality signal** | Accept; mark the record so the UI can group it honestly. | Reserved for reports that are genuinely un-improvable at record time (a check that has no artifact and no command because it was a manual observation), or for historical rows already stored. |
| **A — Aggregate** | Accept; never emit one signal per record. | The condition is a property of a session or a scale, not of one record. |

### How a refusal must read

The existing `_limit_error` helper (`src/agentacct/mcp.py:339-348`) is the model:
it names the limit *and* what arrived, never echoes caller content, and it cost
five blind retries to earn that shape. A refusal for a missing field must be
equally actionable — name the field, say what the section status requires, and
show a copy-pasteable corrected call. Example shape:

```text
section_status=completed requires `summary` describing what actually changed.
Received: no summary. Re-send the same section_id with summary, for example:
  agentacct_record_section(source="codex", section_id="add-rate-limit",
    section_status="completed", section_title="Add rate-limit to login",
    summary="Added a 5/minute limiter and covered it with 3 tests.")
```

No refusal may quote the agent's own text back, and every refusal must be
stateless: the corrected call must succeed as a fresh request.

### Coverage targets

Measured against the real ledger before merge, and re-measured after:

| Metric | Today | Target |
| --- | --- | --- |
| Terminal sections carrying outcome prose | 99.1% | ≥ 99.9% |
| Checks supplying a command or an artifact pointer | 60.6% | ≥ 95% |
| Checks naming a file list | 18.8% | ≥ 60% (soft: `files` is often genuinely unknown) |
| Work items with a real title | 100% | 100%, enforced |
| Usage rows aggregated to session level in attention output | 0% | 100% |


## Implementation status

| Rule | State | Where |
| --- | --- | --- |
| R1 real-text titles | **implemented** | `_display_title` (`src/agentacct/mcp.py`), called at both `section_title` and the `title` alias |
| R2 control-character collapse | **implemented** | `_collapse_display_text` / `_collapse_narrative_text` |
| R4 terminal outcome required | **implemented** | `_require_terminal_outcome`, called before the section context is built |
| R5 reproducible check | **implemented** | `_require_reproducible_check`, measured on the *stored* files list |
| R6 identifiable check name | **implemented** | `_check_has_identity` + `_require_check_identity` |
| R3 `section_id` reuse flag | not yet | needs the read-side group builder |
| R7 usage aggregation | not yet | needs `build_attention_items` and the timeline projection |
| R8 server-side actor resolution | partially exists | the inheritance path already resolves hook context; the missing-key marker needs the new rule |
| R9 idempotency conflict | not yet | the dedupe branch currently returns the stored event silently |
| R10 rules reach every surface | not yet | `_RECORDING_CONTRACT_LINES` not yet updated |

Two behaviours deliberately **not** refused, because the real ledger shows they are
legitimate and refusing them would lose real evidence:

- A check carrying `before_exit_code` / `after_exit_code` with their summaries —
  the CLI and HTTP repair lane records a fix this way and never names a command.
- A generic name when a command, file list or artifact already identifies the
  check, and a section that is `started` or `checkpoint` with no prose.

Validation for the implemented rules:

- `tests/test_data_quality_rules.py` — 20 tests, all passing, covering both the
  refusal and the acceptance direction of every rule.
- Full Python suite: **2,826 passed, 1 failed**. The failure is
  `test_out_of_range_timestamp_never_500s_kept_surfaces`, which fails identically
  at the pristine baseline with this work stashed: it is pre-existing and
  unrelated (a year-10000 formatting helper).


## Rules

### R1. A displayed title must be real text — tier R

**Claim:** `section_title`/`title` must contain at least 2 alphanumeric
characters after whitespace and control characters are collapsed. A title that is
empty, whitespace-only, or punctuation-only is refused.

**Why:** the probe recorded a **completed** section whose title was three spaces,
and another with an embedded newline and tab, both accepted. Measured today: 0
such titles in the store, so this rule costs nothing now and prevents the class.
The display consequence is concrete: an untitled section makes the ledger
substitute the raw `section_id` as the canvas headline
(`src/agentacct/work_ledger.py:4031` → `task_timeline.py:134` →
`WorkTimeCanvas.swift:195`), and the anti-id guard that exists for Task titles
(`src/agentacct/api.py:1064-1072`) does not cover this path.

**Enforcement point:** `_optional_limited_str` callers in the
`agentacct_record_section` branch (`src/agentacct/mcp.py:1444-1445`); the HTTP
`EventRecordRequest` validator (`src/agentacct/api.py:241-247`); `WorkEvent`
post-init (`src/agentacct/work_events.py:198-207`).

**False-positive budget:** 0. The real store has no title that fails it.

### R2. Control characters never reach display — tier N

**Claim:** collapse control characters in single-line display fields
(`section_title`, check `name`, `source`, `client`) to a single space, collapse
runs of whitespace, then strip. Multi-line narrative fields (`summary`,
`blocker`, `next_step`) keep newlines but lose other control characters.

**Why:** the UI renders the title in one clamped line and the TUI truncates by
code points (`src/agentacct/tui.py:297-304`, `:1663-1672`), so an embedded newline
reflows or misaligns rather than displaying. The repair is lossless for meaning;
refusing would be pedantic, because the agent did supply real text.

**Enforcement point:** alongside R1, *before* the length check, so the repaired
value is the one measured against `maxLength` — this is order-dependent and
worth its own test.

**False-positive budget:** 0 rejections by construction; measure that no real
title in the store changes by more than whitespace.

### R3. One `section_id` is one piece of work — tier Q

**Claim:** when a `section_id` is recorded in the same client session with a
*different* title than the one already stored, do not silently overwrite.
Record the new report, flag the item as `section_id_reused`, and name both titles
in the flag.

**Why:** the probe showed "First unrelated task" being replaced by "Second
unrelated task" under one `section_id`, and the projection kept only the second.
The schema already tells agents not to reuse ids (`src/agentacct/mcp.py:215-218`)
but nothing enforces or reports it. Measured: 534 distinct `section_id`s serve
1,167 section events, and the read side already splits composite work keys by
client/session (`src/agentacct/work_ledger.py:702-760`).

**Enforcement point:** the read side already groups by this key, so the flag
belongs where the group is built; the write side can also warn on a title change
within one session. Refusing would be wrong: an agent that fixes a typo in its
own title is legitimate.

**False-positive budget:** title *changes* are legitimate; only report the flag,
never block. Verify against the store that the flag fires on a handful of items,
not hundreds.

### R4. A terminal section must carry its outcome — tier R

**Claim:** `completed` requires a `summary` of at least 40 characters.
`blocked` requires a `blocker` of at least 20 characters; `next_step` is strongly
requested but not required. `handed_off` requires a `summary`. A refusal names
the field the status requires and shows the corrected call.

**Why:** a finished chapter with nothing to read is the single most visible hole
in the timeline — the canvas card shows only title plus "Reported completed"
(`WorkTimelineModel.swift:56-59`), and a missing summary lets a raw `section_id`
surface as a Task objective (`receipt.py:760` → `ReceiptsPane.swift:976`).
Measured today: 5 of 536 terminal sections (0.9%) have no summary, so refusing
costs at most 5 real reports in the whole store while closing the class.

**Enforcement point:** `agentacct_record_section` branch, immediately after
`section_status` is validated (`src/agentacct/mcp.py:1441`), so the check runs
before the context dict is built; mirrored in the HTTP validator and in
`WorkEvent.__post_init__` so every transport agrees.

**False-positive budget:** 0 legitimate terminal reports. The refusal must not
fire for a section that supplies `summary` in any position (metadata is merged
before the check) or when `title` is used as the alias.

### R5. A machine check must be reproducible or point at an artifact — tier R

**Claim:** `agentacct_record_machine_check` requires at least one of `command`,
`artifact_ref`, `artifact_path`, `artifact_url`, or a non-empty `files` list.
`result` may not be *inferred* from `exit_code` when nothing else was supplied:
an inferred `passed` with no command is exactly the record that cannot be audited
later.

**Why:** 64 of 335 real checks have no command, 272 of 335 name no files, and 208
of 335 point at no artifact — yet all 335 count toward evidence, which is how the
ledger reports a 21.7% evidence-backed completion rate while 405 completed items
have no evidence at all. A check that names what it ran or what it produced is
the only kind a reviewer can re-run.

**Enforcement point:** the check branch before the event is built
(`src/agentacct/mcp.py:1106-1260`), including the `exit_code` inference at
`:1184-1187`; `_record_machine_check` must reject rather than synthesize
`"<name>: <result>"` as the only content (`:1188-1193`).

**False-positive budget:** 0. A manual observation that genuinely has no command
and no artifact should be recorded with `agentacct_record_event` (a note), not as
a machine check — the refusal must say so, because that is the honest alternative.

### R6. A check name is an identity, not a default — tier R

**Claim:** `name` may not be the schema default or a bare generic word
(`check`, `test`, `tests`, `verify`, `build`) when it is the only identity the
check has. A short specific name (`pytest tests/test_mcp.py`, `pnpm build:web`)
is required.

**Why:** supersession keys on the name, so two unrelated checks both called
"check" would supersede each other (`src/agentacct/mcp.py:105-108`). The schema
defaults `name` to `"check"` (`:103`), so the failure mode is one omitted argument
away. Measured: 0 generic names in the store today; this is a guard, and it costs
nothing to add.

**Enforcement point:** the check branch, before supersession scope is computed.


### R7. Usage rows are not work items — tier A

**Claim:** imported usage is never emitted as a one-per-turn attention item or
canvas mark. Per-session coverage is one aggregate signal (for example
"2,087 of 2,402 usage rows in 41 sessions have no work context"), and per-turn
rows render only when the user expands a session or asks for usage detail.

**Why:** 2,087 of 2,921 attention items are "usage row has no work context", each
at the same visual weight as the 405 actionable high-severity items; 2,194 of
3,904 timeline rows carry no `work_id`. This is the single largest source of
visual noise and of the explanatory copy the UI needs.

**Enforcement point:** `build_attention_items` (`src/agentacct/work_ledger.py:1606`,
the usage loop at `1661`) and the timeline projection that feeds the canvas.

**False-positive budget:** aggregate must still name the affected sessions and
count exactly; no usage fact may be dropped from the totals.

### R8. An unresolved actor is a defect, not a data-entry request — tier N

**Claim:** `client_session_id` is resolved server-side at record time — from the
Claude Code hook context file, or from the last successful attach on the same
server process — before the section is stored, and `client_context_source`
records which path did it. Only when no server-side source exists is the item
marked `missing_client_session_id`.

**Why:** 16 real items are missing the key, and the shipped guidance already says
never to guess an id (`docs/agentacct-workflow-instructions.md:29`). The 208
attributed items prove the mechanism works when the context is present. Making
agents responsible for a machine-known value is the wrong side of the boundary.

**Enforcement point:** the existing inheritance path
(`src/agentacct/mcp.py:1467-1546`), which already distinguishes
`client_derived_*` from `inherited_*` strategies.

**False-positive budget:** never fabricate; if no source exists the item stays
marked and the UI says so.

### R9. Idempotency conflicts are reported, not swallowed — tier R

**Claim:** if an `idempotency_key` is re-used with content that differs from the
stored event, the call is refused with an error naming the conflicting fields —
or, at minimum, the response states explicitly that the stored event was returned
and the new content was not recorded.

**Why:** the probe showed a second call with the same key and a different title
returning the first event, with the new content dropped silently. Silent loss of
a distinct report is the one failure an agent cannot diagnose. The service
already dedupes on key+source+event_type+run_id (`src/agentacct/service.py:1314-1328`).

**Enforcement point:** the dedupe branch in `SentinelService.record_event`.

**False-positive budget:** 0 for identical replays, which must stay silent and
return the stored event; only differing content is affected.

### R10. Every rule is visible where agents read — tier R (meta-rule)

**Claim:** a rule is not shipped until it appears in (a) the tool schema
description, (b) the rendered `CLAUDE.md`/`AGENTS.md` instructions, and (c)
`docs/agentacct-workflow-instructions.md`. `agentacct mcp doctor` gains a
data-quality section that reports violations read-only.

**Why:** the audit found rules that exist only in code (the `files` path rule in
schemas but not in the instruction docs), semantics that exist only in prose and
are unenforced (`summary` must describe what was "actually observed"), and a
doctor that cannot see any data-quality condition at all. Rendered instruction
files are written once at onboard and never refreshed, which is why the `title`
alias had to be kept forever (`src/agentacct/mcp.py:1414-1418`) — new rules must
reach already-onboarded machines, so the schema descriptions carry the weight.

## Reliability evidence

Three independent checks, all re-runnable, before calling any rule reliable.

### 1. Replay the real store — zero false positives on legitimate work

`tools/audit-agent-data.py --replay` sends every stored section and machine check
back through the live write path and reports what the current rules would refuse.
A refusal is a false positive unless the record is genuinely unusable.

| Measure | Value |
| --- | --- |
| Records replayed | 1,504 |
| Refused | **8 (0.53%)** |
| Completed sections with no summary at all | 5 |
| Checks with no pointer **and no exit code** | 3 |

Every refusal is a record that carries no outcome, no pointer and no objective
anchor — nothing a reader or a re-run could use. **Zero legitimate reports are
refused.** The three checks are specific, well-named integration suites that
recorded only a result word, so even their exit status is absent; that is the
one shape the rule still declines, and it declines it for the right reason.

This replay is what caught the rule being too strict the first time: it initially
refused **11** checks that had a precise name and an exit code but no verbatim
command. Those are real evidence, so R5 now accepts a specific name plus an exit
code.

### 2. Fuzz the validators — 1,415 generated cases

`tests/test_rules_fuzz.py` runs a fixed-seed corpus (hostile literals plus
generated strings across Unicode categories) through the helpers and the real
tools. It asserts three properties:

- **No crash.** Every input either returns a usable value or raises
  `InvalidParams`; a `TypeError` escaping to an MCP client reads as a broken
  tool, not as guidance.
- **Deterministic and idempotent.** The same bytes always normalize to the same
  stored value, and normalizing an already-stored value changes nothing — which
  is what makes a replay or a re-import safe.
- **Total gates.** Every combination of status, summary and blocker either passes
  or refuses; no unhandled branch.

**Two real bugs found by fuzzing** (both fixed, both invisible to the
hand-written tests):

1. **C1 control characters reached the stored text.** The strip set enumerated
   the code points it knew (C0, DEL) and missed U+0080–U+009F, so a title could
   carry characters a renderer shows as nothing or as a replacement glyph. The
   fix asks Unicode for the category (`Cc`) instead of listing code points.
2. **The category fix then ate `\n` and `\t`** — they are `Cc` too — which
   silently destroyed the line structure the narrative rule exists to preserve.
   Caught immediately because the fuzz suite asserts *both* directions: controls
   are gone **and** structure survives.

### 3. Lane agreement — and one honest gap

`tests/test_rules_fuzz.py` also pins the direction that matters: **MCP is never
more permissive than the HTTP model** on a type-level limit. A writer on the HTTP
or CLI lane can still store a record the MCP lane would refuse, because the
completeness rules live in the MCP handler; that gap is asserted explicitly by
`test_handler_rules_are_documented_as_mcp_only`, so it fails the day someone
assumes the lanes are equal — or the day the rules move to the shared choke point
at `SentinelService.record_event` (the better fix, and the one R10 should carry).

## Where rules can be enforced once, per transport

| Surface | Insertion point | Notes |
| --- | --- | --- |
| MCP section | `src/agentacct/mcp.py:1408-1470` | validate before the context dict is built |
| MCP check | `src/agentacct/mcp.py:1106-1260` | `result` inference and summary synthesis live here |
| MCP generic event | `src/agentacct/mcp.py:1310-1338` | accepts `sentinel_semantic_kind`; provenance stripped only |
| HTTP | `src/agentacct/api.py` — `POST /events` 4083, `/work-events` 3805, machine-check 4109 | pydantic models at 208-280 |
| CLI | `src/agentacct/cli.py` — `event record` 5918, `event note` 5965, `outcome record-machine-check` 3708, `evidence work-event` 6353 | CLI truncates and drops unsafe ids to `None` |
| Hook | `src/agentacct/hooks.py` capture functions 646/695/752/453 | fail-open by design |
| **All of them** | `SentinelService.record_event` (`src/agentacct/service.py:1330-1379`) | the single choke point where a cross-surface rule fires once |
| **Type-level** | `WorkEvent.__post_init__` (`src/agentacct/work_events.py:174-209`) | where enum/size rules already fire for every transport |

## Decisions taken

1. **Strictness.** Incomplete reports are refused, not marked, wherever an agent
   can repair them in one retry. The target is that a UI-breaking report
   essentially never reaches the store, and that the refusal tells the agent
   exactly what to add. Tier **Q** survives only for reports that are genuinely
   un-improvable at record time and for rows already stored.
2. **Scope.** All four waves are in scope: display guarantees (R1, R2, R9),
   evidence quality (R4, R5, R6), usage aggregation (R7), and the two extraction
   fixes described in [FINDINGS.md](FINDINGS.md).
3. **Reach.** Rules ship through `_RECORDING_CONTRACT_LINES`
   (`src/agentacct/install_guide.py:282-293`), so MCP `initialize` instructions,
   SessionStart context and the rendered `CLAUDE.md`/`AGENTS.md` block all carry
   them, plus the tool schemas and `docs/agentacct-workflow-instructions.md`.
