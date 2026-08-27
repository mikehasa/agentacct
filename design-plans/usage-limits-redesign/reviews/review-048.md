## Scenario

048 — Duplicate client limit entries arrive with different windows.

## Verdict

Needs a lossless grouping rule.

## Findings

The proposed ledger says one row per recording client. The nearby dashboard exemplar reduces duplicate client entries to one chosen record, which can discard a valid 5-hour or weekly window from another entry.

## Recommendation

Group duplicate entries under the client row and preserve every nonduplicate window as labeled subrows. Surface conflicting same-kind windows separately with account/provenance context; never select one merely by input order or highest use.

## Test idea

Reverse two duplicate entries carrying 5-hour and 7-day windows; assert identical output with both windows and one usage lane.
