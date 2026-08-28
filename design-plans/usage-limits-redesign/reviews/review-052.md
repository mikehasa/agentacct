## Scenario

052 — Partial known-additive cost must retain the tilde grammar.

## Verdict

Pass if the shared formatter remains authoritative.

## Findings

`UsageBucket.costText` sends known-additive cost through `Fmt.costDisplay`, producing `~$`; incomplete period values also retain `~$`. Summary, table, and tooltip consumers display that formatted value rather than recomputing it. The symbol still lacks an explanation.

## Recommendation

Preserve `~$` in every visible and spoken value. Define it once nearby as “known additive subtotal; excludes unpriced or non-additive usage.”

## Test idea

Use incomplete totals, rows, and periods. Assert `~$12.34` everywhere, never bare `$` or `≈$`, and announce “partial known subtotal.”
