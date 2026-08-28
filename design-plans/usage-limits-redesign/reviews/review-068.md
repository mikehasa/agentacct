## Scenario

068 — Thirty-day range changes totals, trend, and breakdown together.

## Verdict

Pass, provided the joined lane uses the same payload.

## Findings

`setUsageDays` fetches plan and usage together, then publishes `usageDays`, totals, periods, and buckets only after both return. The merged ledger adds selected-range client consumption, which must derive from that same 30-day summary. Provider capacity must not change.

## Recommendation

Bind every recorded-usage section to one committed range generation and label the joined lane “last 30 days.” Keep limit windows outside the range transaction.

## Test idea

Switch 7d to 30d with distinct fixtures; assert client rows, totals, chart, and models change atomically while meters/resets remain identical.
