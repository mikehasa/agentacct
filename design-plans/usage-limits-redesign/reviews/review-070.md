## Scenario

070 — Rapid 7→30→90 range switching.

## Verdict

Strong, provided the merged pane keeps the store’s generation gate.

## Findings

`setUsageDays` increments `usageDaysGeneration`, fetches plan and usage together, and publishes only when its generation is newest. This prevents a late 30-day response from relabeling 90-day data. Recreating this join in the view would lose that guarantee.

## Recommendation

Keep range mutation atomic in `DashboardStore`; do not optimistically change the selected label before both payloads land.

## Test idea

Complete mocked 30-, 7-, and 90-day requests out of order and assert the final label, totals, chart, and model rows are all 90-day data.
