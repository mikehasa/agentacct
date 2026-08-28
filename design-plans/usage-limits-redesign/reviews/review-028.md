## Scenario

028 — Usage range request fails while live limit data remains valid.

## Verdict

The store behavior is safe; the UI must expose it.

## Findings

`setUsageDays` commits the range only after both requests succeed, so old usage is not mislabeled. It sets `errorText`, but current Usage never renders that error. Live provider windows are independent of the selected range.

## Recommendation

Keep the previous range selected and consumption unchanged, show an inline range-failure message with retry, and leave Capacity meters active. Do not dim or refetch provider windows merely because ranged usage failed.

## Test idea

Fail a 7d→30d request: assert 7d remains selected, old totals stay labeled 7d, the error appears, and live headroom is unchanged.
