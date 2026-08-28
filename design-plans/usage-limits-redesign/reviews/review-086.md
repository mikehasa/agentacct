## Scenario

086 — Model share misses percentage or token totals.

## Verdict

Needs correction before reuse.

## Findings

`modelShares` displays a dash for missing percentage but computes its bar with `share.pct ?? 0`, visually fabricating zero. Missing tokens also become a bare dash without provenance. The merged disclosure would carry this contradiction forward.

## Recommendation

Use a hatched/unavailable bar for absent percentage, “share not reported,” and “tokens not reported.” Keep an observed 0 distinct from missing.

## Test idea

Render one model with nil percent and tokens, one with explicit zeros, and assert only the latter gets an empty numeric bar and zero labels.
