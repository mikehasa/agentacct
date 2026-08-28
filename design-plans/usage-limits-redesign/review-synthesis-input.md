# Usage & limits review synthesis input

## Corpus validation

- Matrix: exactly 100 numbered scenarios, 001-100.
- Corpus: exactly 100 numbered files, `review-001.md` through `review-100.md`; none missing, no extra numbered review, and no exact duplicate file hash.
- Coverage: every file substantively addresses its matching scenario number.
- Structure: 95/100 files use exactly `Scenario`, `Verdict`, `Findings`, `Recommendation`, `Test idea` as level-two headings. Exceptions are 001 (legacy narrative headings), 002 (legacy prose/bold sections), and 003, 006, 011 (the five standard names at level one). These are format defects, not missing scenario coverage.
- Size: 12,354 words total; 93-315 per file, mean 123.54. Reviews 003-013 are all <=250 words, 014-069 are all <=200, and 070-100 are all <=120. Pre-existing reviews 001/002 are 315/300 words and predate those later contracts.

This validates the review artifacts, not the unimplemented product behavior.

## Reviewer identity reality

The corpus received contributions from **16 distinct scenario-reviewer
identities, not 100 agents**: reviewers 001-014 plus the original reviewers for
052 and 053. Two additional agents performed the cross-surface critique and
coordination. The runtime then reached its cumulative distinct-agent ceiling.
Three existing reviewers consequently simulated the remaining blocks:
reviewer 005 wrote 015-039, reviewer 009 wrote 040-069 (and condensed 052/053
to the final format), and reviewer 008 wrote 070-100.

The corrected ledger is the final authorship census. Conclusions should be
treated as 100 scenario probes from 16 scenario-reviewer identities, with
correlated authorship inside the three simulated blocks.

## Ranked high-confidence consensus

Counts below are overlapping review-presence counts used for ranking, not votes.

1. **Never turn absence, invalidity, or mixed provenance into a number** — 58 reviews; critical. Preserve nil versus observed zero, unpriced/partial/reported cost grammar, stale/invalid states, lossless custom windows, and explicit non-invoice/non-budget copy. Do not join unnamed capacity to unattributed usage. Core scenarios: 8-10, 21-30, 34-49, 51-60, 65-66, 74, 77-88.
2. **Accessibility and non-pointer operability are part of the data contract** — 49 reviews; high. Provide complete combined VoiceOver summaries, keyboard chart inspection, stable focus/order/identifiers, non-color threshold text, localization, large-text layouts, and deliberate announcements. Core scenarios: 3-4, 13-20, 35, 41-47, 62-64, 69, 87-93, 98-99.
3. **Live provider capacity and selected-range recorded usage must remain independent lanes** — 43 reviews; critical. Give them separate labels, freshness, errors, and transactions; range changes must never alter provider windows or publish mixed generations. Core scenarios: 1-3, 7-10, 23-29, 31-40, 65-75, 79, 84, 94.
4. **The joined ledger needs deterministic, lossless identity and scale behavior** — 36 reviews; high. Join the union of usage and limit clients, preserve multiple windows and duplicate/stale siblings, use stable IDs and lazy rows, and sort measurable risk separately from unknown capacity. Core scenarios: 5, 22-24, 31-35, 45-50, 55, 60-64, 73-74, 80.
5. **Remove plan share as a page mode, but preserve calibrated facts as client-scoped detail** — 17 reviews; high for truth, lower visual priority. Distinguish calibrating, terminally incompatible, absent-old-schema, sparse/missing series, unknown-time share, and multi-week accumulation. Core scenarios: 1-2, 6-7, 32, 76-86.
6. **The merge direction itself is accepted, but is not release-ready.** No review argues for retaining two top-level navigation tabs. Scenarios 95-100 make routing, stale-code removal, deterministic references, docs, tests, build, and live accessibility smoke checks release gates.

## Conflicts and outliers

- **Scenario 011** proposes a sticky internal Usage/Limits switch to solve 960x560 height. This conflicts with the dominant continuous decision-first ledger and risks recreating the removed mode fork. Prefer a compact complete first capacity row, visible scrolling, and live minimum-window testing; adopt a switch only if that stop condition fails.
- **Scenario 006** wants cost to lead; 001, 005, 007, 031, and the candidate lead with capacity. Resolve with Capacity now first but compact, cost present in joined rows, and the ranged total/trend immediately after it—neither concern should hide the other.
- The plan places stale readings and calibration in About; **024, 047, 077-083, and 092** require actionable status/counts inline or client-scoped. Put methodology/definitions in About, but never bury current absence, incompatibility, hidden-count, or calibration progress.
- **Scenario 012's** three-column card suggestion is based on the old Limits grid and conflicts with the full-width ledger signature. Retain its centering/max-width finding, not the card grid.
- **Scenario 097** and current source contradict the preliminary plan's assumption that a menu limit click-through already exists: menu limit rows are static. This is new implementation scope within the stated navigation goal.
- Reviews 001/002 and 003/006/011 are formatting outliers; their substantive findings remain usable.

## Implementation checklist

- [ ] Replace the two top-level panes with one Usage destination and decision-first capacity ledger; retain a compact, complete first row at 960x560 (1-7, 11-13, 21-24, 31-35, 94).
- [ ] Build a feature-local joined-row model over the union of identities; preserve multiple windows, stale/fresh siblings, unnamed/unattributed isolation, stable IDs, lazy rendering, and deterministic risk/unknown ordering (5, 22-24, 33-35, 45-50, 60-64, 73-74, 80).
- [ ] Keep capacity and consumption source state separate: timestamps, loading/error copy, disconnected/incompatible handling, and a shared generation gate for refresh/range requests (1-3, 7-10, 25-29, 67-75).
- [ ] Centralize truth formatting and tri-state derivations; correct zero-fill, elapsed reset, negative percent, 75/90/100 copy, missing-share bars, identity fallbacks, and invoice/budget caveats (8-10, 34-45, 49, 51-60, 65-66, 86-88).
- [ ] Move plan facts into client-scoped disclosure without losing progress, basis, fixed today/7d values, sparse/missing-series meaning, unknown-time share, or >100% multi-week labeling (76-86).
- [ ] Implement labeled controls, chart keyboard/focus interaction, combined accessibility summaries, focus rings/order/IDs, stale-count announcements, quiet polling, localization, large text, Increased Contrast, Reduce Motion, and Reduce Transparency (3-4, 13-20, 47, 69, 87-93).
- [ ] Route semantic `.limits` destinations to `.usage` while clearing stale task/session selection; add the missing menu limit action; remove `MainPane.limits` and the orphan pane shell (95-97, 100).

## Test and release checklist

- [ ] Fixture-state matrix: empty, usage-only, limit-only, connecting, disconnected, incompatible, stale, missing percent/reset, elapsed reset, invalid percent, duplicate/unnamed entries (21-50).
- [ ] Usage-truth matrix: unpriced, partial/reported/estimated cost, missing tokens/sessions, cache-heavy, tiny/billion values, anonymous buckets, nil versus zero days (51-66).
- [ ] Async/range tests with controlled completion order, paired failures, minute-refresh overlap, scroll/focus preservation, and distinct freshness (67-75).
- [ ] Calibration fixtures for every state and sparse/missing field combination (76-86).
- [ ] Keyboard and VoiceOver tree tests plus live smoke checks for charts, disclosures, stale toggle, status announcements, and semantic row summaries (3-4, 87-93).
- [ ] Deterministic light/dark minimum/reference snapshots, Increased Contrast and accessibility-text variants, placeholder-free controls, 90-day density, and 100-client/500-model performance (11-20, 50, 61, 64, 69, 98-99).
- [ ] Navigation matrix for dashboard and menu semantic limit routes, four-tab count, selection clearing, stale-code/docs/assets search, full Swift tests, release build, and final diff audit (95-100).
