## Scenario

008 — An analyst wants precise provenance and no fabricated values when fields are missing.

## Verdict

Needs revision. The candidate has unusually good missing-state language, but one derived metric still turns absence into false precision.

## Findings

The cost grammar preserves reported, estimated, partial, and unpriced states; summary cells name absent values; and a limit without `usedPercent` gets “used % unreported” plus a hatched track. Those choices support analyst trust.

However, `UsageBreakdownTable` computes both ordering and share with `freshTokens ?? 0`. If one row lacks tokens while another has them, the missing row says “none” yet receives a numeric `0%` share. That percentage was not reported or derivable. Likewise, the synthetic label “unknown” conflates a missing client/model identity with a real identifier literally named “unknown.” The merged pane also needs provenance and freshness attached to each data family, since ranged receipts and live client limits are not one observation.

## Recommendation

Keep missing, observed zero, and positive values distinct through every derivation. Render an unavailable share as “not reported,” exclude it from the known-token denominator, and disclose denominator coverage. Label absent identities “client not reported” or “model not reported.” Preserve separate source/basis and refreshed-at text for usage, cost, and limit readings.

## Test idea

Render rows with 100 tokens, explicit zero tokens, and `nil` tokens plus a `nil` identity and unpriced cost. Assert only the explicit zero gets `0%`, no `$0` appears, and every missing value names its provenance.
