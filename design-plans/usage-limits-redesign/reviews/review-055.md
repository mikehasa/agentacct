## Scenario

055 — Cost exists while token count is absent.

## Verdict

Needs absence semantics.

## Findings

The current UI can show the cost, but labels absent tokens as “none recorded” or “none” and computes a 0% token share. Missing is not zero; the row can therefore look internally contradictory or be demoted in usage ordering.

## Recommendation

Show the cost unchanged, label tokens “not reported,” and render share as unavailable. Do not substitute zero for sorting or joining; use another explicit deterministic tie-breaker.

## Test idea

Create a costed row with nil tokens. Assert cost remains visible, tokens and share are unavailable, and no zero-token claim appears.
