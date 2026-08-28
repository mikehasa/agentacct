## Scenario

064 — Hundreds of models make the model breakdown unwieldy.

## Verdict

Needs progressive disclosure.

## Findings

The current model table eagerly renders every row in one `VStack`. Hundreds of models create a very tall page, delay access to “About these numbers,” and make refreshes expensive. The merge plan carries this table forward without a density rule.

## Recommendation

Show a ranked top set plus an explicit “Other models” aggregate and “Show all” control backed by lazy rows. Compute totals and shares from the full dataset.

## Test idea

Load 500 models; verify prompt initial render, accurate top/Other shares, full expansion, stable focus, and reachable disclosure.
