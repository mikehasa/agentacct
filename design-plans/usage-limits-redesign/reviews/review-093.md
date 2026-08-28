## Scenario

093 — Useful, non-noisy refresh announcements.

## Verdict

Current spinner semantics are insufficient.

## Findings

The top bar labels an active spinner “Refreshing local data,” but exposes no completion or failure announcement. Glance polls every 30 seconds and dashboard data every 60; announcing every cycle would be disruptive, while silence after a user range change hides outcome.

## Recommendation

Keep background polling silent. Announce user-initiated range completion/failure and connection-state transitions, coalescing simultaneous usage and capacity updates.

## Test idea

Run two background polls, one manual refresh, and one failed range switch; assert only the manual outcome and failure produce status announcements.
