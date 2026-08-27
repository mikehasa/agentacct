## Scenario

017 — Reduce Transparency enabled.

## Verdict

Supported if the merged pane preserves existing surface policy.

## Findings

`WindowSurfacePolicy` already replaces window material with opaque `Theme.canvas` when Reduce Transparency is active. Cards and meter tracks are solid colors, so the proposed ledger does not require translucency.

## Recommendation

Build the ledger and About disclosure from the existing opaque tokens. Avoid adding material-backed sticky headers, popovers, or translucent row washes; disclosure state must remain readable against the solid canvas.

## Test idea

Open the merged pane with Reduce Transparency on in light and dark modes, expand About, and verify no desktop bleed-through or lost surface separation.
