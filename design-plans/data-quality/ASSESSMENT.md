# Does this app actually help the user? An honest assessment

Written 2026-09-12 after the rules/choke-point/extraction work in this branch.
Every number below is measured from the installed store (8,369 events, 534 work
items) or from the code; nothing here is a projection.

This is deliberately not a summary of what shipped. It is an attempt to answer
the questions that matter, including where the answer is bad.

## 1. Is the agent reporting the information the user needs?

**Largely no — and the reason is the contract, not the agent.**

What agents report today is prose: 27 words, 2 sentences, no citations. What the
user needs to decide whether to trust a claim is something checkable. The ledger
supports that in principle and almost never uses it:

| Verifiable pointer | Checks carrying it (of 336) |
| --- | --- |
| `exit_code` | 328 |
| `command` | 273 |
| `artifact_path` | 119 |
| `files` | 65 |
| `artifact_url` | 11 |
| **commit / SHA / diff / PR reference** | **0** |

So the app can say "a test command exited 0 at some point." It cannot say *what
changed* or *which revision* was tested. Nothing in the recording contract asks
for it: `artifact_ref` and friends are accepted by the schema, described nowhere,
and used in 0-35% of checks.

The honest framing: **the app currently reports claims with a confidence label,
not facts with a citation.** "Evidence-backed completion rate 21.7%" is the app's
own admission of that.

## 2. What does the user actually see?

Three real completed items, verbatim from the ledger:

- *Find and fix native app lag* — strong evidence, 7 checks, 5 named files, a
  207-character summary. **This is genuinely useful**, and it is the shape the
  product should produce every time.
- *Reuse the shared timeline in the native app* — 14 checks, evidence status
  **failed**, no files, no usage. A reader cannot tell whether the work succeeded.
- *Remove the duplicate menu bar app* — no checks, no files, no usage, a
  one-sentence summary. Nothing here is verifiable.

Across all 534 work items, the shape is:

| | Count |
| --- | --- |
| Completed with **no checks at all** | 405 (76%) |
| Completed with **no attributed usage** | 324 (61%) |
| **Items where a reader has checks AND cost AND a high-confidence join** | **11 (2%)** |

A user opening the app today therefore sees, for most rows: a title, a prose
summary, "completed", and a footnote explaining why nothing else is available.
The explanatory copy I measured earlier (20+ phrasings for "this field is
missing") is not clutter the designers chose — it is the app apologising for the
data, row after row.

And the app's own verdict is on screen: `ledger_health: poor`, "415 completed
work item(s) lack strong machine-check evidence". That is an honest signal, but
it describes **historical** records the user cannot fix. Without a date boundary
("since we started enforcing this") it reads as a permanent red mark on their
whole history.

## 3. How does each feature help?

| Feature | Intended help | Honest verdict |
| --- | --- | --- |
| Task list | What work happened | Works. Real titles, real grouping. The 4 "Untitled" rows showed the fallback was inventing labels; fixed. |
| Evidence tiers (self/independently/externally verified) | Separate claim from proof | **Mostly inert.** 76% of completed items have no check to tier, so the distinction rarely appears. Valuable where it does. |
| Machine checks | Prove the work ran | The most concrete feature — name, command, exit code. Undermined by 0 commits/PRs and 208 checks with no artifact. |
| Usage attribution | Cost per unit of work | **Broadly unavailable.** 91.3% of usage rows have no work context; 308 items are ambiguous. Cost-per-task is a promise the data does not keep. |
| Attention queue | What needs my review | **Counterproductive at current volume.** 2,921 items, 2,087 of which are one per-turn bookkeeping notice, against 405 genuinely actionable. |
| Timeline canvas | Read the shape of a session over time | Works well as a view of *recorded events*; it cannot show what it was never told. |
| Receipts | A shareable summary | Depends entirely on the above; currently shares prose plus a low evidence rate. |

## 4. Is it easy to read? Is there enough information?

Reading is fixable and partly fixed in this branch. **Enough information is not.**

- The canvas card shows title + lane + result, so the outcome lives one click
  away, and the inspector renders a 1,000-character summary as one block.
- The TUI inlines a 207-character summary into a single terminal line — the worst
  text decision in the product.
- Structure is preserved but never rendered: agents write 0% bullets because
  nothing asks for them and nothing would show them.

Volume is not the problem — 27 words is a reasonable length. **Sourcing is the
problem**: there is nowhere in the prose that says where the claim came from.

## 5. What pain does the app actually solve?

Genuinely, today:

1. **"What did the agents do while I was away?"** — answered, across five clients,
   locally, without cloud access. This is real and it is the core value.
2. **"What did this cost?"** — answered at the session/provider level, honestly
   labelled as reported vs estimated, with an explicit refusal to claim
   subscription billing.
3. **"Which work is unverified?"** — answered, but at a rate (21.7%) that makes
   the answer "almost all of it".

Not solved, despite appearances:

4. **"Was this work done correctly?"** — the app can show a passing check exists;
   it cannot show what was checked against. Without a revision reference, a
   passing check from last week and one from five minutes ago look the same.
5. **"What should I review?"** — the attention queue cannot answer this because
   routine bookkeeping outnumbers real findings 7:1.

## 6. Where I over-built, and what I would do differently

Honest self-assessment of this branch:

- **The title requirement (R1/T) is a small win I spent real effort on.** It
  closes the "Untitled" path, which is worth doing, but 0 of 534 sections were
  untitled anyway. The effort-to-user-visible-improvement ratio is poor.
- **Requiring a summary on terminal sections (R4) targets 8 records in 1,504.**
  Correct and cheap, but it will not change what the user sees for months.
- **The evidence rule (R5) is the one that actually matters**, because 208 checks
  with no artifact and 64 with no command are what make "verified" meaningless.
- **The fuzzing found real bugs** (C1 controls reaching stored text; the category
  fix eating newlines). That was worth it.
- **The choke-point move was architectural hygiene, not user value.** It prevents
  future divergence; it changes nothing a user sees today.
- **I did not touch the two biggest user-facing problems**: the attention queue's
  7:1 noise ratio, and the absence of any revision/commit reference in the
  recording contract. Both are bigger wins than everything above, and I stopped
  short of them.

And the uncomfortable part: **most of my rules only affect data written from now
on.** The 534 existing items stay as they are. I improved the app's future
without improving the user's present, and I should say so rather than let the
test counts imply otherwise.

## 7. What would actually make this app more helpful, ranked

1. **Get a revision reference into the ledger — mechanically, not by asking.**
   Verified: no `commit`/`diff`/`sha` field exists in any recording surface, and
   the codebase captures no git context anywhere (`git rev-parse` appears
   nowhere). Adding a field an agent self-reports would invite a plausible
   invented SHA — the exact failure the Claude/Codex identity work exists to
   avoid. The right shape is capture, not request: the hooks and the CLI already
   see the repository, so the checked revision can be recorded from the
   environment the check actually ran in. That is more work than a schema field,
   which is precisely why it is worth stating as the top item rather than
   assuming it is a one-liner. One trustworthy revision turns "a check passed"
   into "this revision passed this check". Nothing else on this list comes close.
2. **Collapse the attention queue to actionable items only.** Aggregate per-turn
   usage notices per session (R7, designed and not built), and separate
   "needs a human decision" from "data is thin". The queue should be able to be
   empty.
3. **Bound health claims by time.** "Poor" over three months of legacy data is
   noise; "since 12 Sep: 4 of 5 completed items carry a check" is a signal the
   user can act on.
4. **Render the structure the contract asks for** (whitelist bullets/paragraphs
   in the inspector, wrapped summary in the TUI) — cheap, visible, and it makes
   the writing guidance worth following.
5. **Be explicit about the cost gap in the UI.** Say "cost per task is
   unavailable for 91% of usage" once, at the top, instead of a footnote per row.
6. **Finish the counting copy, in both languages at once.** The receipt still
   prints `1 checks · 1 passed · 0 failed` and `touched 3 file(s)` /
   `ran 1 command(s)`; the coverage ledger says `1 step(s) ran in subagents` and
   `2 check(s) attach to no step`. Eight Python sites
   (`receipt.py:545,551,554,936`, `receipt_markdown.py:100,102`,
   `cli.py:9773,9776`) and, for the same rows, four in the app
   (`V1Model.swift:771,773,774`, `ReceiptsPane.swift:110`) — the one-vocabulary
   rule means they change together or not at all. The app already carries the
   pattern to copy: `ReceiptsPane.swift:463` pluralizes
   (`pathCount == 1 ? "path" : "paths"`). Why it is filed rather than half-done:
   `dashboard.json:554-555,1472` holds ledger counts of exactly 1, so corrected
   wording changes what the Work page draws, and the 98 canonical reference
   images can only be re-recorded on macOS 26.6 (25G72) — this host is 26.5.1,
   so the change would be unverifiable here and would land blind in CI.

## 8. The one-sentence answer

The app reliably answers *what happened* and honestly refuses to answer *was it
right*; the fastest way to make it genuinely helpful is to capture the checked
revision mechanically, because that one fact is what turns the ledger from a
record of claims into a record of verifiable work.

## 9. What I am not claiming

- The 4,253 passing tests (4,348 on this PR's head) prove the rules behave as
  designed. They do not prove the rules were the most valuable thing to build. By
  the ranking in §6, they were not.
- "Zero false positives on 1,504 replayed records" measures that the rules do not
  reject legitimate work. It says nothing about whether the work was legitimate.
- The evidence rate, the usage attribution and the attention ratio are properties
  of what agents record. No amount of app-side validation fixes them; only the
  recording contract and the client integrations can.
- I verified the UI question through the data the app renders and through one
  live API call that surfaced the four identical "Untitled" rows. I did not
  visually inspect the running app, so claims about layout and legibility rest on
  reading the view code, not on looking at the screen.


---

# Addendum: three direct questions, answered with measurements

Added 2026-09-12. This section corrects two numbers in the body above that came
from measuring the wrong layer, and reports what the content of the prose
actually says.

## Correction first

I previously reported "0 sections carry a client_session_id" and "21.7% cost
attribution". The first was measured on raw section EVENTS rather than the
resolved ledger, and it was misleading: the ledger resolves a session for 522 of
538 work items and log-evidences 519 of them. Only 5 section events literally
carry the field. Both numbers are true of different layers; the item-level figure
is the one the UI shows. The 61%-unjoined figure does still hold.

## Q1 — Is the data top quality? Structurally yes; evidentially no

Per work item (n=538):

| Field | Coverage |
| --- | --- |
| Title | 100% |
| Summary | 98.9% |
| Project | 98.1% |
| Session resolved | 97.0% |
| Names files | 69.1% |
| Attributed cost | 39.0% |
| **Has a passing check** | **21.9%** |
| **Has next_step or blocker** | **0.7%** |

Per machine check (n=339): `exit_code` 97%, `command` 81%, `artifact_path` 35%,
`files` 19%, `artifact_url` 3%, `artifact_ref` 0%, and **0 commit or PR
references**. Checks are correctly tied to their section (339 of 339).

So the *shape* of the data is good — the fields that exist are filled and
well-linked. The *evidence* is thin: a completed item usually claims completion
without a check, and no check anywhere names a revision.

## Q2 — Do agents use it perfectly? The mechanics yes, the reporting no

Discipline is genuinely strong, and better than I expected:

- 538 sections opened, **535 closed** (99.4%) — the lifecycle is respected.
- Every check names its section; none floats free.
- After the client fixes, Codex records at 516 items with 99.8% completion.

But the prose is the problem, and it is measurable. Across 820 summaries, what
the first sentence actually does:

| First sentence | Share |
| --- | --- |
| Describes what the agent DID (process) | ~41% |
| Session/review notes, "other" | ~37% |
| Restates the status ("Completed…") | ~20% |
| **States what CHANGED** | **~3–15%** (see note) |

Note on the range: the strict classifier finds 2.9%; folding in the explicit
change verbs the contract now asks for (`Saved`, `Created`, `Implemented`,
`Added`) raises it to roughly 15%. Either way it is a minority, and the modal
summary is a process list: *"Inspected live Dashboard, Work receipt, Usage"*,
*"Read the bounded protocol, assigned manifest"*, *"Reviewed setup/upgrade, CI,
dependency state"*.

That last one is the clearest illustration: after reading it, a user still does
not know whether the review found anything. The agent recorded its activity
faithfully and omitted the finding.

**So: agents use the mechanism well and report the wrong thing.** They are
recording *what they did*; the user needs *what changed*. The contract asks for
the latter and mostly receives the former — which is a prompt problem, not an
agent-quality problem, and it is the single highest-leverage thing left to fix.

## Q3 — Is it displayed in the best fashion? No, and the mismatch is specific

| Surface | What it shows | The mismatch |
| --- | --- | --- |
| Canvas card | Title, lane, result; `lineLimit(2)` | A 40-character title is made to carry the meaning, while the outcome — the thing the user wants — is one click away and, per Q2, usually absent anyway |
| Inspector | The full summary as one unbounded `Text` | Renders a process list as a wall; the one surface with room for structure does not use it |
| TUI check row | Summary inlined into the headline (`tui.py:2053`) | A 207-character median summary on one terminal line |
| Receipt | Prose plus a low evidence rate | Shares claims without citations |

And the app spends its scarce space explaining what is missing: 20+ distinct
phrasings for "this field is missing", which is apology copy for the gaps that
Q1 and Q2 describe.

## What this changes about priorities

The ranked list in §7 stands, with one correction of emphasis. Requiring a
revision reference remains first. But the cheapest large win is now clearer:
**teach the contract to demand the finding, not the activity.** A summary that
says what was discovered or changed is worth more than any additional field,
because it is the only content a reader actually consumes — and today it is
missing in the majority of records.
