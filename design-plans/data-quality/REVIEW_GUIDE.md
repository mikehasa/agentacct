# Review guide

The PR diff is large for a structural reason, not because the change is large.
This file says exactly where to look, in what order, and what to skip.

## Why the diff looks huge

This branch is **stacked** on `design/native-recording-experience` (draft PR
#190), because the work depends on files that exist only there —
`task_timeline.py` and the Codex hook bridge among them.

Stacking has a cost GitHub cannot hide here: **stacked pull requests require all
branches in the same repository, and cross-fork stacks are not supported**
([GitHub docs](https://docs.github.com/en/pull-requests/get-started/about-stacked-prs)),
so the base cannot be set to the branch this actually builds on. The diff
therefore shows the stack's whole history alongside this work.

| What a reviewer sees | What this PR actually contributes |
| --- | --- |
| 167 files, ~28,650 insertions | **70 files, 5,218 insertions** |
| (includes 302 commits from the branch below) | 10 product files, ~1,100 lines |

Two commands show the real contribution:

```sh
git diff --stat ac233b9..HEAD          # 70 files
git log --oneline ac233b9..HEAD        # this work only
```

## Read these, in this order

**1. The rules themselves — `src/agentacct/semantic_rules.py` (new, 367 lines)**

This is the whole change in one file. Read it first and decide whether the rules
are right; everything else is plumbing to make them apply. Each rule states the
measurement that justifies it.

**2. The enforcement point — `src/agentacct/service.py` (+48 lines)**

One function, `_enforce_semantic_rules`, called from `record_event`. Every write
lane funnels through it, which is why the rules cannot be bypassed. The `if`
guards are the interesting part: what is deliberately *out* of scope.

**3. The bug fix — `src/agentacct/hooks.py` (+120 lines)**

The Codex `SessionStart` hook and the client-aware context validator. This is
where the headline defect lived: a validator hardcoded to `claude-code` discarded
every Codex context, so 1,161 sections recorded without a session id.

**4. The rest of the product code — four small files**

| File | Lines | What to check |
| --- | --- | --- |
| `mcp.py` | +405 | Mostly schema descriptions. Skim: the descriptors are prose, the logic is ~40 lines |
| `client_usage.py` | +56 | Codex revision watermark; Claude identity budget 256 KiB → 2 MiB |
| `display_budget.py` | +100 (new) | Surface budgets and their derivation; check the arithmetic |
| `install_guide.py` | +39 | The recording contract, rewritten; note the line budget it must respect |
| `api.py`, `task_timeline.py`, `receipt_markdown.py` | +52 | Display fallbacks: a sentence instead of a clipped paragraph |

## Skip these

**44 test files, 1,503 added lines against 85 removed** — mechanical fixture
completion. The new rules require fields most fixtures never carried, so a tool
inserted them (`design-plans/data-quality/tools/complete-test-fixtures.py`, 274
lines, worth reading once because it is the thing that touched 44 files). The
churn is `"summary": ...` and `"section_title": ...` lines. Diffing it as a whole
is not a good use of review time.

**10 design and evidence files** under `design-plans/data-quality/`. The
one to read is `RULES.md`; the rest is measurement.

## The four test suites worth reading

| Suite | Tests | What it proves |
| --- | --- | --- |
| `test_data_quality_rules.py` | 22 | Every rule in both directions: refuses the incomplete record AND accepts the complete one |
| `test_rules_fuzz.py` | 11 (1,417 cases) | Validators never crash; normalization is deterministic and idempotent; all three lanes agree |
| `test_text_hygiene.py` | 10 | Source-level guards: placeholder-copy and `unknown`-fallback inventories cannot grow silently |
| `test_display_alignment.py` | 18 | Budgets match the surfaces; the schema discloses them; the real store measured through the display path |

## Verify it yourself rather than trusting the description

```sh
python3 design-plans/data-quality/tools/verify-fixes.py    # 30 checks, exits non-zero on failure
```

It drives the real code paths a client drives and prints every value it observed
— including the before/after of the session-link fix and the upgrade path for a
machine that already has the old Codex config. Output is committed at
`design-plans/data-quality/evidence/VERIFICATION.txt`.

## What would make this reviewable *and* mergeable

The stack is the problem, not the change. Options, in order of preference:

1. **Merge #190 first.** Then this becomes a small standalone PR against `main`
   — 10 product files, no stack, no 28k-line diff. This is the outcome worth
   asking for.
2. **Open with the base branch**, which removes the stack from the diff. Not
   possible from this fork, because cross-fork stacks are unsupported.
3. **Review commit by commit** if the diff is too much at once — the commits are
   ordered so each one is a coherent step, and each keeps the suite green.
