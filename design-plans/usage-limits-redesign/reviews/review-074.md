## Scenario

074 — Usage and limits have different freshness.

## Verdict

The proposed source separation is necessary but underspecified visually.

## Findings

Usage freshness comes from `DashboardStore.lastUpdated`; limit freshness comes from `GlanceState.lastUpdated`. The current Usage header shows only the former, Limits shows neither, and the top bar labels dashboard time as generic “Local data.” A joined row could imply one synchronized observation.

## Recommendation

Show separate “usage refreshed” and “capacity refreshed” timestamps in the ledger header or lanes; retain stale badges per limit reading.

## Test idea

Render usage updated now with limits updated 12 minutes ago and assert both times remain visible and separately labeled.
