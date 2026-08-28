## Scenario

082 — Calibrated client has no daily plan series.

## Verdict

Needs a named chart absence.

## Findings

Current code silently omits `PlanDailyChart` when `daily` has fewer than two points, while still showing headline and model shares. In a disclosure, that omission could look like a layout bug or collapsed content.

## Recommendation

Preserve the valid today/7d estimates and state “Daily plan series not reported” where the chart would be. Do not synthesize zero days.

## Test idea

Render a calibrated client with window shares and `daily = nil`; assert shares remain and an explicit no-series message replaces the chart.
