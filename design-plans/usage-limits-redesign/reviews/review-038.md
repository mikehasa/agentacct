## Scenario

038 — Reset happens within the next six days.

## Verdict

Good baseline, with localization and boundary checks needed.

## Findings

The existing formatter uses abbreviated weekday plus time for dates under seven days. That is compact and decision-friendly, but the merged ledger needs enough width and an unambiguous accessibility value across month boundaries.

## Recommendation

Retain locale-formatted weekday and time visually; expose the full localized date/time to assistive technology. Recompute from the current local calendar rather than persisting a stale relative label.

## Test idea

Test resets one through six days away across month and daylight-saving boundaries; assert weekday correctness and no collision with selected-range usage.
