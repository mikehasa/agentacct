## Scenario

066 — Period has missing tokens but should not render as zero.

## Verdict

Visual distinction passes; wording fails.

## Findings

The token chart renders nil as a neutral stub and numeric zero as a minimal colored bar, but its detail says “none recorded.” Breakdown rows similarly say “none,” which can be read as a measured zero.

## Recommendation

Say “tokens not reported” for nil and “0 tokens” only for numeric zero. Preserve distinct styling and expose the distinction in accessibility text.

## Test idea

Render adjacent nil, zero, and positive token days; assert different markers and the exact phrases “not reported,” “0 tokens,” and the positive count.
