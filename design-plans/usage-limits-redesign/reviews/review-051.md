## Scenario

051 — Fresh tokens and sessions exist but no priced usage exists.

## Verdict

Truthful values, weak default chart.

## Findings

The current summary says “no priced usage” and rows say “unpriced,” correctly avoiding $0. However, the daily chart defaults to cost, so an otherwise active range opens as neutral stubs until the user switches to tokens.

## Recommendation

When every period is unpriced, default the chart measure to Tokens. Keep Cost selectable with an explicit “No priced usage in this range” state, and retain token/session totals.

## Test idea

Load active token periods with all costs nil; assert no zero dollars, Tokens opens by default, and Cost explains the absence.
