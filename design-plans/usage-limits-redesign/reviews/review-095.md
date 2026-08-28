## Scenario

095 — Dashboard View limits deep link.

## Verdict

The plan covers it; tests must enforce the semantic redirect.

## Findings

Dashboard currently calls `selection.open(.limits)`, which clears stale task/session state and opens `.limits`. The candidate correctly keeps that semantic destination while routing it to `.usage`, avoiding changes at every caller.

## Recommendation

Change only the `.limits` destination mapping to the merged Usage pane and retain the existing button identifier. Update the destination matrix expectation.

## Test idea

Seed stale task and session IDs, activate View limits, and assert both clear while pane becomes `.usage` and Capacity now is visible.
