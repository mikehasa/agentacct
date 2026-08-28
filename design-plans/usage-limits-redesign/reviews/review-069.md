## Scenario

069 — Ninety-day range produces dense daily bars.

## Verdict

Needs interaction adaptation.

## Findings

The current `HStack` gives every day equal width with a fixed three-point gap. Ninety bars fit visually, but at the minimum window their individual hover targets become tiny and the density approaches noise.

## Recommendation

Reduce gaps adaptively and use one chart-wide pointer/keyboard hit area that resolves the nearest date. Preserve all 90 daily values and a sequential accessibility representation; do not silently bucket days.

## Test idea

Render 90 varied days at 960 points; verify legibility, first/middle/last labels, nearest-day inspection, keyboard access, and no clipped bars.
