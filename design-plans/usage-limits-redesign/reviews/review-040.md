## Scenario

040 — Reset timestamp has already passed.

## Verdict

Needs revision.

## Findings

`resetsAtText` returns nil for an elapsed timestamp, and the current row then says “Reset time unreported.” That erases a reported fact and makes stale provider data look merely incomplete. The merged ledger would repeat this misleading state beside current capacity.

## Recommendation

Render “Reported reset passed [absolute time]” and flag the reading for refresh or staleness evaluation. Do not infer that reset occurred or that capacity renewed.

## Test idea

Use a reset one minute ago; assert elapsed wording and the absolute timestamp, never “unreported” or an upcoming reset.
