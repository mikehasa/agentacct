## Scenario

026 — Daemon disconnects while cached usage remains visible.

## Verdict

Preserve usage, but sharply separate its freshness from capacity.

## Findings

`DashboardStore` retains the last usage summary after refresh failure, while `GlanceState` replaces its snapshot with `.disconnected`. The merged surface can therefore show historical consumption safely, but no current headroom.

## Recommendation

Keep totals, trend, and breakdown visible with their last-updated time. Replace capacity meters with “Live limits unavailable—daemon disconnected” and an error/retry affordance; never let the page-level freshness imply both lanes are current.

## Test idea

Load both lanes, then force disconnection. Assert usage values remain, all live meters disappear, and separate freshness/error labels are visible.
