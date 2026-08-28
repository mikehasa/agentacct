## Scenario

022 — Recorded usage exists but no client reports limits.

## Verdict

The joined ledger handles this well if absence stays explicit.

## Findings

The current Limits pane derives usage-only clients and names “No limits reported.” The merged row can improve this by keeping each client’s ranged tokens, cost, and sessions beside that absence. It must not equate missing limits with unlimited capacity.

## Recommendation

Create rows from usage clients even when the limit set is empty. Render “Provider limit not reported,” omit the meter/headroom calculation, and place these rows in a clearly labeled unknown-capacity group.

## Test idea

Provide three usage clients and zero limits; verify three attributed rows, unchanged totals, no meters, and no unlimited/healthy wording.
