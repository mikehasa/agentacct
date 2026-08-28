## Scenario

077 — Calibrating with zero clean intervals.

## Verdict

Needs more progress evidence.

## Findings

The models expose `intervalsUsed` and `intervalsNeeded`, but current UI only says “calibrating from your own limit history” or renders `stateDetail`. With zero intervals, that can look stalled or imply evidence already exists. The merged disclosure must distinguish waiting for the first clean interval from ordinary progress.

## Recommendation

Render “0 of N clean intervals observed” and the daemon’s state detail; never show a percent or meter until calibrated.

## Test idea

Fixture `calibrating`, used 0, needed 3; assert the count and learning state appear, with no plan-share value.
