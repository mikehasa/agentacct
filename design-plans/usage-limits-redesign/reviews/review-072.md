## Scenario

072 — Minute refresh overlaps a range switch.

## Verdict

Concurrency needs one more contract.

## Findings

`refresh()` guards its own reentry, but `setUsageDays()` runs independently. A refresh started after the generation increments but before `usageDays` commits can still fetch the old range and briefly publish it. The range request should win without flicker or clearing its error.

## Recommendation

Serialize range and periodic usage lanes, or give every usage/plan fetch a shared request generation and commit rule. Glance capacity polling can remain independent.

## Test idea

Pause a 30-day switch, trigger refresh, complete old-range refresh first, then the switch; assert no stale commit or error reset occurs.
