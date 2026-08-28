## Scenario

099 — Light/dark references preserve hierarchy and truth.

## Verdict

Not release-ready: references do not cover Usage or Limits today.

## Findings

The canonical reference set covers Dashboard, Work, menu, and About, while `SnapshotRunner`’s all-pane images are not gated visual baselines. The merged signature ledger therefore lacks a durable light/dark review contract.

## Recommendation

Add merged Usage minimum/reference images for both appearances using identical fixture states and viewport hierarchy. Assert more than palette difference: named absence, stale state, threshold labels, and order must match.

## Test idea

Compare semantic fixture markers plus pixels across the four references and fail if either appearance drops or reorders a truth state.
