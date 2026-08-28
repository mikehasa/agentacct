## Scenario

014 — Light appearance with low-contrast card boundaries.

## Verdict

Needs revision.

## Findings

The candidate’s quiet, no-shadow system depends heavily on surface and line contrast. In the light palette, card versus canvas is only 1.13:1, cardLine versus card is 1.40:1, and hairline versus card is 1.31:1. The full-width capacity ledger, totals, chart, and breakdown can therefore collapse into one white/beige field even though their text remains legible. Existing contrast tests cover text, not structural boundaries.

## Recommendation

Keep the flat aesthetic, but use the stronger semantic rule for the ledger perimeter and major section breaks; `#79848B` reaches 3.83:1 on white. Reserve hairlines for rows already grouped by that structure, and avoid restoring per-row card clutter.

## Test idea

Add a populated deterministic light snapshot plus a palette test requiring key structural boundaries to reach 3:1 against adjacent surfaces.
