# Review guide

Read this first. It says where to look, in what order, and what to skip.

## Shape of the change

**74 files, 3 commits**, against `main`:

| Commit | What it is |
| --- | --- |
| `0a03525` | The rules, the enforcement point, the Codex session-link fix, extraction, display alignment — and the test/fixture updates they require |
| `dabb93c` | The design record and three measurement tools |
| the last commit | This guide, plus the one unfilled placeholder the receipt copy still carried |

The **eleven product files are 1,127 insertions**. Everything else is tests, docs
and tooling. The two code commits pass the full suite on their own — 4,348 tests
each, verified in a clean worktree — and the head runs **4,349** after the
placeholder guard.

This PR replaced #193, which was stacked on the branch that #190 later merged.
The stacking was the only reason that one showed 168 files; nothing was lost in
the move.

## Read these, in this order

**1. `src/agentacct/semantic_rules.py` (new, 367 lines)**

The whole change in one file. Read it first and decide whether the rules are
right; everything else is plumbing to make them apply. Each rule states the
measurement that justifies it and the false-positive budget it must meet.

**2. `src/agentacct/service.py` (+48)**

One function, `_enforce_semantic_rules`, called from `record_event`. Every write
lane funnels through it, which is why no surface can store what another refuses.
The guards are the interesting part: what is deliberately *out* of scope, and why
a refusal there would drop a fact instead of correcting a report.

**3. `src/agentacct/hooks.py` (+94/−26)**

Where the headline defect lived: no Codex `SessionStart` hook, and a context
validator hardcoded to `claude-code` that discarded every Codex context anyway.
Measured consequence — 1,161 sections with zero session ids, and 61% of work never
joining to usage.

**4. The rest of the product code**

| File | Lines | What to check |
| --- | --- | --- |
| `mcp.py` | +377/−28 | Mostly schema text. The two substantive blocks are the client-context validator and the `agentacct_work_status` implementation — both near the end of the diff |
| `client_usage.py` | +54 | Codex revision watermark; Claude identity budget 256 KiB → 2 MiB |
| `display_budget.py` | +100 (new) | One budget per surface, each with its derivation. Check the arithmetic |
| `install_guide.py` | +34 | The recording contract, rewritten. Note the line budget it must respect |
| `api.py`, `task_timeline.py`, `receipt_markdown.py` | +48 | Display fallbacks: a sentence instead of a clipped paragraph |
| `receipt.py` | +5/−2 | The coverage definition said `X of Y checkable steps`. Now it says what it means, and a rendered receipt is tested for placeholder-shaped tokens |

## Skip these

**40 existing test files, +305/−86.** The rules require fields most fixtures never
carried, so a tool inserted them — about seven lines per file, mechanical
throughout. Read `complete-test-fixtures.py` once instead of diffing the 40 files.

**The design record** under `design-plans/data-quality/`. `RULES.md` is the one to
read if you want the full rule catalogue; the rest is measurement you can
regenerate.

**Seven `docs/` files: five one-liners, plus two regenerated examples.** The five
add the new tool's name to the live-tool inventory the docs-drift test enforces;
the two worked examples are re-rendered from the receipt engine, which is also
why they are not hand-edited.

## The four new test suites — 1,218 lines, 1,467 tests

These are where the test diff's weight actually is. Everything else is fixtures.

| Suite | Tests | What it proves |
| --- | --- | --- |
| `test_data_quality_rules.py` | 22 | Every rule in both directions: refuses the incomplete record AND accepts the complete one |
| `test_rules_fuzz.py` | 1,417 | Validators never crash; normalization is deterministic and idempotent; all three lanes agree |
| `test_text_hygiene.py` | 10 | Source-level guards: placeholder-copy and `unknown`-fallback inventories cannot grow silently |
| `test_display_alignment.py` | 18 | Budgets match the surfaces; the schema discloses them; the real store measured through the display path |

## What this PR does not prove

State these as limits, not as things to find. They are measured, and they are in
`ASSESSMENT.md` in full.

- **No live Codex session has run through the new hook.** The fix is verified
  against the code paths and against recording a section that inherits Codex
  context, not against a real Codex turn. The hook also has to be reinstalled on
  a machine before it takes effect; that is the upgrade path check, not a claim
  that any machine is already fixed.
- **The rules only bind new writes.** The 9 stored records they refuse stay
  stored. Nothing here rewrites history, and no past work becomes more complete.
- **The card budget is a geometric estimate.** 54 characters is derived from the
  card's measured width and type, not from screen capture, and it is stated to
  ±10%. 28 of 538 live titles (5.2%) exceed it and clip.
- **Summary quality is the biggest remaining gap and this PR does not close it.**
  Roughly 41% of stored summaries restate process, ~20% restate status, and only
  ~3–15% state an outcome. The rules require a summary to exist and to be
  readable; they cannot require it to be useful.
- **Nobody has visually reviewed the app after these changes.** The alignment
  work is argued from the store through the display path, and the snapshots pass.
  That is not the same as a person looking at the canvas.
- **The counting copy is still wrong, and it is filed rather than fixed.**
  Receipts print `1 checks · 1 passed · 0 failed`, `touched 3 file(s)`,
  `1 step(s) ran in subagents`. It spans eight Python sites and four in the app
  — the one-vocabulary rule changes them together — and the reference images
  that draw it can only be re-recorded on macOS 26.6. Ranked as item 6 in
  `ASSESSMENT.md` §7 with the exact lines.

## Verify it yourself rather than trusting the description

Both commands below import `agentacct`, so they need the project environment —
`.venv/bin/python -m pip install -e . pytest` per `CONTRIBUTING.md`. A bare
system `python3` has no dependencies installed and stops at `import fastapi`.

```sh
.venv/bin/python design-plans/data-quality/tools/verify-fixes.py    # 30 checks, exits non-zero on failure
```

Drives the real code paths a client drives and prints every value it observed,
including the before/after of the session-link fix and the upgrade path for a
machine that already has the old Codex config. Output committed at
`design-plans/data-quality/evidence/VERIFICATION.txt`.

To watch the loop a user and an agent are actually in — the refusal, the retry
that fixes it, what the card and the receipt then render — and to see the search
for what is still wrong:

```sh
.venv/bin/python design-plans/data-quality/tools/demo-data-quality.py
```

Parts 1-3 are assertions and exit non-zero if any stops holding;
`tests/test_data_quality_demo.py` runs them in CI. **Part 4 is a falsification
run, not a certificate:** it goes looking for remaining defects and prints what
it finds, with live counts from the installed ledger. Read Part 4 before
believing Parts 1-3, and see "What this PR does not prove" below.

To check the rules against real data rather than fixtures:

```sh
.venv/bin/python design-plans/data-quality/tools/audit-agent-data.py --replay
```

It sends every stored record back through the live write path. On the installed
store that is 9 refusals out of 1,541 (0.58%), all genuinely incomplete — zero
legitimate reports refused. The denominator grows as the ledger fills; the
refused count does not. A rule that refused real work would show up here as a
number rather than an opinion.
