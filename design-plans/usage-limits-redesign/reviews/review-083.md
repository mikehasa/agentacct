## Scenario

083 — Sparse or missing plan dates.

## Verdict

The existing chart is misleading for this input.

## Findings

`PlanDailyChart` gives every returned point equal spacing, so 1 Aug and 8 Aug appear adjacent just like consecutive days. Its axis shows only first, middle, and last entries, hiding missing dates. Moving it into About does not fix the temporal distortion.

## Recommendation

Build a calendar-domain sequence with explicit missing-day gaps or label the chart “reported days only.” Missing days must not become zero bars.

## Test idea

Supply dates 01, 02, and 08; verify a six-day gap or explicit sparse-series warning is visible.
