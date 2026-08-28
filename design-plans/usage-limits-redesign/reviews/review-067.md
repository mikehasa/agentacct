## Scenario

067 — Seven-day range is the default decision window.

## Verdict

Pass.

## Findings

`DashboardStore.usageDays` defaults to 7, and the merged decision explicitly retains one 7/30/90 control while removing the blank-prone plan mode. Seven days is therefore available immediately without saved-preference state.

## Recommendation

Keep 7d selected on a fresh store and app relaunch. Apply it only to recorded consumption, totals, trend, and breakdown; provider windows remain independent.

## Test idea

Launch with populated and empty fixtures; assert 7d is selected, all usage labels agree, and provider window/reset values are unchanged.
