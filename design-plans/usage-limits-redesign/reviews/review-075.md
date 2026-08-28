## Scenario

075 — Understandable relaunch default.

## Verdict

Pass.

## Findings

`DashboardStore.usageDays` initializes to seven days, and the merged design removes the data-dependent plan/dollar mode. Thus reopening Usage has a stable decision window and cannot land on the current plan mode’s explanatory near-empty state. This matches the design’s “Last 7 days” hierarchy.

## Recommendation

Keep 7d as an unsaved default; do not restore an obsolete mode preference. Ensure direct limit routes also open the same 7d merged pane.

## Test idea

Create a fresh store with no calibrated clients, open Usage, and assert 7d is selected with Capacity now and usage totals visible.
