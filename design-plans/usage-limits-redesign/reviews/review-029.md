## Scenario

029 — Glance refresh fails while ranged usage remains valid.

## Verdict

Needs a capacity-only failure presentation.

## Findings

A glance failure moves `GlanceState` to disconnected, while ranged usage can remain valid. Treating this as a whole-page failure would discard useful local history; continuing to show old meters as live would be worse.

## Recommendation

Keep the selected-range totals, chart, and models interactive. Replace Capacity with a scoped refresh error; if the last snapshot is retained later, mark every meter stale with its own timestamp rather than silently caching it.

## Test idea

Start populated, fail only glance refresh, then change the usage range successfully. Verify the new usage renders while capacity remains explicitly unavailable or stale.
