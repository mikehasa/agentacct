## Scenario

032 — One live five-hour rolling window with no weekly window.

## Verdict

Works if weekly semantics are not inferred.

## Findings

The current view names a `5h` kind “5-hour window,” but the merged row could invite comparison with selected-range consumption or calibration. Neither creates a weekly plan denominator.

## Recommendation

Show “5-hour rolling window,” its percent and reset as provider facts, and use that window for headroom ordering. Omit weekly plan-share copy and state plainly in About that the selected usage range is independent.

## Test idea

Fixture only a 5h window and change usage ranges; verify no Weekly label or plan percentage appears and the provider meter stays constant.
