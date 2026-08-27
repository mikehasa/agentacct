## Scenario

061 — One hundred usage clients stress joined-row ordering.

## Verdict

The ordering contract needs completion.

## Findings

The design orders by least live headroom, but many usage-only clients have no comparable percentage. The current eager breakdown and offset-sensitive patterns also risk churn at this scale.

## Recommendation

Render every client once in a lazy ledger: valid fresh headroom ascending, then unavailable/stale capacity; within ties sort usage descending, then client name. Use stable IDs and never treat missing headroom as 0%.

## Test idea

Shuffle 100 mixed clients repeatedly; assert identical order, exact row count, preserved focus, and no missing usage-only clients.
