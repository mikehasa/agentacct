## Scenario

054 — Complete pricing-table estimate must retain the approximation marker.

## Verdict

Pass if formatting stays centralized.

## Findings

For `cost_complete: true` with estimated confidence, `Fmt.costDisplay` returns `≈$`, not bare `$`. Totals, periods, and breakdown rows currently consume that grammar, and the merged plan explicitly preserves it. Completeness describes coverage, not billing certainty.

## Recommendation

Retain `≈$` for every complete pricing-table estimate and expose “pricing estimate” adjacent to the headline plus in accessibility text. Never promote it because all rows were priced.

## Test idea

Render complete estimated totals, days, clients, and models; assert `≈$12.34` everywhere and no bare dollar value.
