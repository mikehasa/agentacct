## Scenario

056 — Tokens exist while session count is absent.

## Verdict

Pass.

## Findings

The summary and breakdown already render the token value while naming sessions “not reported.” The merged selected-range lane can preserve both facts without hiding the client or manufacturing a zero-session count.

## Recommendation

Keep token totals, token share, and cost independent of session availability. Use “Sessions not reported” in visible and accessibility text, including compact joined rows.

## Test idea

Provide 12,345 fresh tokens with nil sessions. Assert the client remains ranked by tokens, displays 12.3k, and never shows 0 sessions.
