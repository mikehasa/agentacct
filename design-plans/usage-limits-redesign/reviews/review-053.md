## Scenario

053 — Complete client-reported cost may use a bare dollar figure.

## Verdict

Needs a label correction.

## Findings

`Fmt.costDisplay` correctly permits bare `$` only for complete reported or billed values. Yet the current and proposed fixed label “Est. cost” contradicts that stronger provenance, even when the adjacent qualifier says “client-reported.”

## Recommendation

Use neutral “Cost” labels wherever reported and estimated values can mix. Keep “client-reported” adjacent to the aggregate, and allow bare `$` only when that displayed aggregate is wholly complete and reported.

## Test idea

Render complete client-reported totals, periods, and rows. Assert bare `$12.34`, neutral headings, and visible plus spoken “client-reported” provenance.
