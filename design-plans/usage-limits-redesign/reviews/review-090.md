## Scenario

090 — Stable focus order and identifiers.

## Verdict

Not specified enough.

## Findings

Current range and measure pickers use empty labels and no accessibility identifiers. The new About disclosure and stale control add more focusable elements, while adaptive stacked ledger rows can change visual order at minimum width.

## Recommendation

Define one invariant order: range, capacity rows, stale control, measure, chart, disclosure. Add stable identifiers for range choices, measure choices, stale toggle, and About expansion; preserve semantic order when rows stack.

## Test idea

Tab through 1172pt and 960pt layouts and assert the same logical order and identifiers in both.
