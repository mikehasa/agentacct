# Session and steps readability: scenarios 68-100

> Read-only specialist simulation lane: **33 scenarios, not 33 independent agents**. These cases are structured role-play against `origin/main` at `89c34f8`; they are design/test inputs, not observed usability-study results.

## Scope and limitations

- This lane inspected the screenshot, SwiftUI models/rendering, deterministic fixtures, snapshot harness, API projection, and CI. It did not edit production code or run a human study.
- Scenarios exercise the data/truth/testing boundary: zero-to-many sessions and steps, 0/1/25/100 checks, lifecycle/result/history states, provenance, malformed data, responsive layouts, ordering, completeness, and performance.
- `/v1/session` is a complete, nonpaginated detail payload today. Progressive UI disclosure must not be described as backend pagination.
- WCAG sources below inform native-app accessibility decisions but do not by themselves establish macOS conformance. Final confidence requires semantic tests, canonical images, assistive-technology checks, and live-app review.

Priority: **P0** truth/data-loss blocker; **P1** core readability/accessibility; **P2** polish.

## Scenarios

| # | Role and context | Goal | Current failure or risk | Invariant / design requirement | Priority |
|---:|---|---|---|---|:---:|
| 68 | Compliance reviewer opens a legacy receipt with zero linked sessions. | Determine whether session detail is unavailable. | The section silently disappears, suggesting the receipt is complete. | Name the absence: “No linked session details available”; never claim no work occurred. | P1 |
| 69 | Developer opens a receipt with one root session. | Understand its scope before expanding. | Root chip, title, project, and recency compete on one line. | Session title leads; role/project/time are secondary and reflow without hiding the title. | P1 |
| 70 | Maintainer reviews primary plus continuation roots. | Reconstruct chronology. | Repeated cards and small chips make root boundaries weak. | Primary/continuation headings visibly group members and preserve server order. | P1 |
| 71 | Engineering lead reviews one root plus 20 subagents. | Find the relevant reviewer. | A tall undifferentiated card stack is slow to scan. | Show exact session/subagent totals, stable labels, and bounded progressive disclosure without losing any member. | P1 |
| 72 | User expands a valid session containing zero recorded steps. | Distinguish an empty recording from failure. | Muted lowercase copy resembles a loading residue. | Use an explicit empty state distinct from loading and failure; do not imply the session did no work. | P1 |
| 73 | Agent is working in one started step with zero checks. | Monitor progress. | Evidence pip and absent count can resemble completed-but-unverified work. | Lifecycle says “In progress”; absence of checks is named without calling it failure. | P0 |
| 74 | Reviewer sees completed, blocked, handed-off, and started steps together. | Locate action needed. | Lifecycle chips and evidence tiers have similar visual weight. | Blocked/current work is identifiable before claimed/completed work; lifecycle and evidence remain separate axes. | P0 |
| 75 | Documentation step has file claims but zero checks. | Assess confidence. | “Claimed” can be mistaken for validation. | Say files were reported but no passing check was recorded. | P1 |
| 76 | Release engineer sees one passing client-hook check. | Confirm observed success. | A tiny icon carries the result. | Visible “Passed” text, exit code, and “hook” source stay together; green is permitted only because the result is machine-observed. | P0 |
| 77 | Reviewer sees one agent-reported pass. | Judge independence. | A checkmark plus “passed” summary can imply independent proof. | Show “Passed · agent-reported”; never green or “verified.” | P0 |
| 78 | PR author has 25 current agent-reported passes. | Identify what was run. | Repeated provenance pills dominate 25 flat rows. | Show a digest plus bounded rows and exact “Show 19 more current checks”; silently drop nothing. | P1 |
| 79 | CI owner has 100 mixed checks. | Find failures without rendering a wall. | The full eager stack harms scanning and render cost. | Show current failures first, then newest current results; provide exact hidden count and reachable expansion for every record. | P1 |
| 80 | On-call engineer has one active failure older than many unrelated passes. | Locate the blocker. | Newest-first chronology can bury the only actionable result. | Digest says “1 failing”; the active failure appears in the initial visible group regardless of age. | P0 |
| 81 | Developer reruns the same check; the old failure is superseded by a pass. | Distinguish present state from history. | “25 checks” treats historical failure like current evidence. | Separate current from history; place the old failure only under “History/Superseded,” paired by identity when possible. | P0 |
| 82 | Auditor encounters `supersession_state=unconfirmed`. | Decide whether the failure remains actionable. | The UI does not name the state; it may look historical. | Unconfirmed failure remains current/actionable and never enters superseded history. | P0 |
| 83 | A later unrelated pass follows a failure with another identity. | Avoid a false recovery conclusion. | Recency can imply the failure was fixed. | Preserve identity boundaries; an unrelated pass never clears or visually demotes the active failure. | P0 |
| 84 | Build engineer compares `failed` and `error`. | Understand the failure mode. | Both use the same x-mark with little textual hierarchy. | Preserve exact “Failed” and “Error” labels; both count as current failures and retain their summaries. | P0 |
| 85 | Tester sees skipped, unknown, nil, or future results. | Avoid an invented outcome. | A generic dot gives no semantic name. | Show “Skipped” or “Result unknown”; never infer pass from exit code or color. | P1 |
| 86 | Manager opens a running/started step whose check has not completed. | Separate lifecycle from evidence. | Missing result can look like failure or completion. | Step says “In progress”; use “Pending/unknown” only when supported by the wire—never invent “running.” | P0 |
| 87 | Completed step contains a current failing check. | Recognize a claim/evidence contradiction. | “Completed” can overpower the failure. | Failure digest and row remain prominent; completion is lifecycle, not proof. | P0 |
| 88 | A localized task has a 300-character step title. | Identify the step. | Collapsed title truncates before distinguishing words; trailing chips consume width. | Prioritize the leading title, allow a bounded two-line preview, and expose full text when expanded or via help. | P1 |
| 89 | Security check summary contains long paths, URLs, and multiline diagnostics. | Read the decisive detail. | The two-line cap hides the tail. | Expanded summary is untruncated and selectable; metadata moves to a separate wrapping line. | P1 |
| 90 | Check has no summary. | Understand what is and is not recorded. | The row renders a suspicious blank after its type. | Show “No summary recorded”; keep evidence type/result/source visible. | P1 |
| 91 | Legacy check lacks `source_type`. | Assess provenance conservatively. | The model asserts “agent-reported” without evidence. | Display “Source unknown” and grant no independent/green styling. | P0 |
| 92 | One step mixes CI, hook, agent-reported, and unknown sources. | Compare independence. | Repeated similar pills obscure real differences. | Keep concise source text per row; never collapse mixed sources into a stronger digest tier. | P0 |
| 93 | Malformed record says `passed` with exit 1, or `failed` with exit 0. | See the contradiction without data rewriting. | Conflicting fields appear without warning. | Show both verbatim with a neutral “inconsistent result” cue; rewrite neither. | P0 |
| 94 | Auditor needs partial/full resolution and artifact context. | Follow the evidence trail. | Resolution/artifact fields are decoded but hidden by the row. | Preserve concise resolution text and safe artifact reference in optional detail; never expose raw commands. | P1 |
| 95 | Legacy payload has missing or duplicate event/work IDs. | Keep rendering stable. | UUID fallback changes identity each access; duplicate IDs make `ForEach` behavior undefined. | Use deterministic, occurrence-safe rendering identity, preferably enumeration at the view boundary. | P0 |
| 96 | Checks arrive stale or reordered after refresh. | Compare state without losing context. | Identity churn and implicit ordering can move focus or imply new semantics. | Preserve server order within groups, use stable identity, disclose relevant time, and never call result-grouping chronology. | P0 |
| 97 | Session payload is malformed, schema-incompatible, 404, or temporarily unavailable. | Recover safely. | Generic “couldn’t load” has no recovery and conflates causes. | Distinguish failure from empty/loading, provide accessible Retry, retain old loaded content during refresh, and reject late overwrites. | P0 |
| 98 | User sees six rows from a complete 25-check payload. | Know whether more records exist. | Disclosure may look like backend pagination or a complete six-item list. | Say “Show 19 more,” not “next page”; total derives from the complete payload and expansion reveals all 25. | P0 |
| 99 | Low-vision/RTL user uses a 320–360 pt content width at accessibility text size. | Read and operate every truth field. | Horizontal metadata overflows; visual and focus order can diverge. | One-axis reflow, no clipped truth fields, logical chevron→title→details focus, text in addition to shape/color, RTL-safe alignment. | P1 |
| 100 | Analyst uses a 1,800 pt window on 20 steps × 100 checks. | Compare quickly without UI stalls. | Long lines are hard to track and an eager 2,000-row layout is expensive. | Cap readable width, align rows, render bounded initial results, expand on demand, and never imply records were omitted. | P1 |

## Root-cause trace

1. **The wire already carries the needed truth.** `/v1/session` returns complete newest-first steps/checks with result, `source_type`, supersession state, timestamps, exit code, resolution, and safe artifact fields. No API pagination or schema redesign is required.
2. **The row layout flattens the hierarchy.** `CheckRow` puts type, summary, exit, provenance, and history in one baseline-aligned `HStack`; the summary is capped at two lines despite the expanded-detail comment promising no truncation.
3. **Aggregation is ambiguous.** `checks.count` includes superseded history, so “25 checks” does not say what is current, failing, or historical.
4. **Truth cues are visually weak.** Result is mostly a tiny glyph while repeated muted provenance chips dominate; agent-reported passes demand careful decoding.
5. **Unknown data is overinterpreted.** Missing/future `source_type` falls through to “agent-reported,” and missing step/check IDs produce fresh UUIDs on access.
6. **Empty and failure states are underdesigned.** Zero sessions removes the section; load failure is generic text without Retry.
7. **Visual coverage misses the target.** Existing 800-point receipt snapshots stop above the below-fold Sessions and steps section; no focused renderer protects dense checks.

## Minimal implementation seam

No daemon/API change is necessary.

- Add a view-neutral `StepCheckDigest` and presentation helpers beside `StepCard`: total records, current, failing, passing, other, superseded, conservative source/result labels, and ordered current/history collections.
- Replace ambiguous counts with exact summaries such as `24 current · 1 history` and `2 failing · 22 other current`, including singular forms.
- Initially show about six current checks, guaranteeing active failures are represented. Use exact `Show N more current checks`; place superseded records in a separately collapsed History group.
- Split `CheckRow` into two semantic levels: explicit result/type/full selectable summary, then exit/source/time/resolution/history metadata. Switch metadata from horizontal to stacked at narrow widths.
- Use deterministic occurrence identity in `ForEach` rather than UUID model fallbacks.
- Give zero-session, zero-step, loading, and failure states distinct copy; add Retry for session load failure.

## Red-test matrix

Add focused semantic tests before UI edits for:

- current versus superseded counts; `unconfirmed` remains current;
- active failure prioritization without treating unrelated passes as recovery;
- stable server order within current/history groups;
- six-row default and exact hidden counts for 7, 25, and 100;
- missing/future source → `Source unknown`, never independent;
- agent-reported pass never receives independent green;
- explicit failed/error/skipped/unknown labels and missing-summary fallback;
- contradictory result/exit-code detection;
- singular/plural digest copy;
- duplicate/missing IDs rendered with occurrence-stable identity;
- distinct empty/loading/error/retry session states;
- existing API guarantees: complete newest-first projection, safe curated fields, and no raw command.

## Snapshot and CI plan

Add a focused `SessionStepsSnapshotRenderer`; the full-page Work references do not show this section. Render light/dark for:

- sparse: one session, one unverified step, zero checks;
- mixed lifecycle/evidence/source states;
- dense 25: long summaries, active failure, mixed sources, one superseded record;
- dense 100 collapsed: exact summary and bounded initial rows;
- compact 360 pt;
- accessibility type plus RTL;
- empty, loading, error, and Retry states.

Verification sequence:

1. Deterministic double-render with independent expected filenames and dimensions.
2. Focused semantic Swift tests.
3. `swift test` and `swift build -c release`.
4. Canonical `./Scripts/visual-snapshots verify WorkVisualRegressionTests`.
5. Inspect every expected, actual, and diff PNG in light/dark before recording.
6. Live-daemon smoke using a real 25-check receipt.
7. Keyboard traversal, Reduce Motion, accessibility text, and narrow/wide resize.
8. Include the focused matrix in the CI review artifact.

Guard specifically against false-green agent claims, active failures hidden as history, superseded records counted as current, unknown provenance asserted as agent-reported, raw-command leakage, unstable IDs, two-dimensional scrolling, truncated expanded summaries, misleading pagination copy, visual crop, and late-load overwrites.

## Primary sources

- Apple, [Disclosure controls](https://developer.apple.com/design/human-interface-guidelines/disclosure-controls): keep essential information visible and disclose related detail progressively.
- Apple, [Layout](https://developer.apple.com/design/human-interface-guidelines/layout): use hierarchy, alignment, grouping, adaptability, and reading-order-aware layout.
- Apple, [Lists and tables](https://developer.apple.com/design/human-interface-guidelines/lists-and-tables): express hierarchy clearly and make long collections manageable without implying missing data.
- Apple, [Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility): minimize complexity and preserve familiar, consistent interaction.
- W3C, [Use of Color](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color): pair color with shape or text.
- W3C, [Info and Relationships](https://www.w3.org/WAI/WCAG21/Understanding/info-and-relationships.html): preserve structural relationships programmatically.
- W3C, [Reflow](https://www.w3.org/WAI/WCAG21/Understanding/reflow): avoid loss or two-dimensional scrolling at constrained width.
- W3C, [Focus Order](https://www.w3.org/WAI/WCAG22/Understanding/focus-order.html): keep keyboard order meaningful and operable.
- W3C, [Headings and Labels](https://www.w3.org/WAI/WCAG22/Understanding/headings-and-labels.html): use concise labels that describe topic or purpose.
