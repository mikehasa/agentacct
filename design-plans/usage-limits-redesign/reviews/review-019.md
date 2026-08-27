## Scenario

019 — Large accessibility text wraps labels and KPI values.

## Verdict

Needs a size-aware layout contract.

## Findings

Current summary and table primitives use fixed column widths, fixed header heights, and one-line qualifiers. The plan’s width-based row stacking alone will not protect large text; it can clip resets, cost basis, or client names while the viewport remains wide.

## Recommendation

Switch the capacity row and total strip to stacked, flexible-height layouts at accessibility sizes. Remove one-line constraints from truth-bearing copy and keep capacity before consumption in reading order.

## Test idea

Snapshot the 960×560 pane at an accessibility dynamic type size with long values; verify one complete row is readable without clipping or overlap.
