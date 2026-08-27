## Scenario

057 — Cache-read tokens dwarf fresh tokens.

## Verdict

Pass, with a proximity risk.

## Findings

Current KPIs and shares use fresh tokens, and the basis footer states how many cache-read tokens were excluded. The merged wireframe shortens the lane to an unlabeled token figure, which could obscure that basis when cached volume is enormous.

## Recommendation

Label the lane “fresh tokens” and retain the excluded cache-read count in nearby basis copy or “About these numbers.” Never add cached tokens into shares.

## Test idea

Use 1M fresh and 9B cache-read tokens; assert 1.0M drives totals/shares and 9.0B appears only as excluded context.
