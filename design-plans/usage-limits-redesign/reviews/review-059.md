## Scenario

059 — Billion-scale token totals test layout and precision.

## Verdict

Compact display passes; precise recovery is missing.

## Findings

The formatter keeps KPI width stable with one-decimal `B` notation, but values such as 1.04B collapse to 1.0B. The fixed-width table fits, yet a decision surface should not make the exact recorded count unrecoverable.

## Recommendation

Use compact text visually and expose the grouped exact integer in accessibility and hover/help text. Preserve 64-bit arithmetic for totals and shares.

## Test idea

Test 999,999,999; 1,040,000,000; and 9,876,543,210 for fit, correct compact rounding, exact spoken values, and stable shares.
