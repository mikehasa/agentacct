## Scenario

025 — Connecting to the daemon on first open.

## Verdict

Requires coordinated initial loading copy.

## Findings

Today, Limits says “Connecting…” while Usage can say “No usage loaded,” producing contradictory first impressions. In the merged pane, absent data during the initial request is not yet an empty or unreported fact.

## Recommendation

Render the page shell and a stable Capacity placeholder labeled “Connecting to local data…”. Delay empty-state and “limit not reported” conclusions until both initial lanes resolve. Disable range changes only while their first request is pending.

## Test idea

Delay glance and usage responses independently; verify no empty claim flashes and each lane becomes usable as its response arrives.
