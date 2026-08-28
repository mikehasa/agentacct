## Scenario

047 — Multiple stale accounts are disclosed and toggled on.

## Verdict

Promising, but the merged disclosure is underspecified.

## Findings

The existing Limits pane reports the hidden count and names, then labels every revealed card “stale reading.” The merged plan moves stale data into “About these numbers” but does not define a result count, reveal scope, or focus behavior.

## Recommendation

Show “3 stale readings hidden,” make the toggle reveal only stale capacity rows, announce the resulting count, and retain a stale badge plus timestamp on each row.

## Test idea

Toggle three stale readings on and off by keyboard; verify count, focus stability, announced result, and no change to fresh-row ordering.
