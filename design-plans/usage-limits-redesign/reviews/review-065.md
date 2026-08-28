## Scenario

065 — Period has missing cost but should not render as zero.

## Verdict

Pass.

## Findings

The daily chart keeps nil cost out of its scale, draws a neutral stub, and announces “unpriced.” A numeric $0 day remains a separate plottable value. This is the correct truth model for the merged chart.

## Recommendation

Preserve nil through charting, peaks, totals, and tooltips. Keep the neutral missing marker and “unpriced” accessibility text; never coalesce nil to zero during range joins.

## Test idea

Place a nil-cost day between $0 and $1 days; assert three distinct visual/spoken states and a $1 maximum.
