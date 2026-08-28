## Scenario

050 — One hundred limit-reporting clients stress ordering and rendering.

## Verdict

Viable only with lazy, stable rows.

## Findings

The current limit grid is lazy, but the merged full-width ledger does not specify its container or identity. Rebuilding 100 meter rows eagerly or identifying them by offsets risks refresh churn, lost focus, and visible reorder jumps.

## Recommendation

Use a `LazyVStack`, stable domain-derived row IDs, and one deterministic sort: valid live headroom first, unavailable/stale states after, then a stable name tie-breaker.

## Test idea

Refresh 100 shuffled clients repeatedly; verify identical ordering, one row per client, stable focused row, and acceptable render/scroll performance.
