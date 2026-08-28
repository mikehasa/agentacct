## Scenario

080 — Multiple calibrated clients.

## Verdict

Requires client-scoped details.

## Findings

`UsagePane` currently iterates every calibrated client. A single global About disclosure could mix bases, intervals, and plan shares, especially when client names repeat across capacity rows. The joined ledger already provides the right ownership boundary.

## Recommendation

Attach each calibration summary to its client row, with a global explanation only for shared terminology. Preserve deterministic client ordering and do not aggregate unlike weekly plans.

## Test idea

Provide two calibrated clients with different bases and shares; verify each detail is announced and visually grouped with the correct client.
