## Scenario

058 — A single tiny usage row tests compact-number formatting.

## Verdict

Pass.

## Findings

`UsageTotals.compact` preserves exact integers below 1,000, so a one-token row remains “1” rather than “0.0k.” With one row, the proportional share is correctly 100%. The merge should not introduce a minimum rounded quantity.

## Recommendation

Keep exact small integers, monospaced alignment, and a visible minimum share bar without altering its 100% label or underlying value.

## Test idea

Render one-row fixtures at 1, 9, and 999 tokens; verify exact text, 100% share, and unchanged accessibility values.
