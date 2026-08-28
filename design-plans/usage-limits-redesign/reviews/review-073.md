## Scenario

073 — Refresh while scrolled deep.

## Verdict

Likely safe, but joined-row reordering is a new risk.

## Findings

The current `ScrollBox` is not keyed to payload changes, so ordinary publishes should retain its offset. The proposed capacity ledger sorts by least headroom; a poll can reorder or resize rows above the viewport, shifting the model section under the reader.

## Recommendation

Use stable client identities, avoid refresh-driven `.id` changes, and preserve the visible anchor when capacity ordering changes. Do not auto-scroll on successful polling.

## Test idea

Scroll to the tenth model row, refresh with reordered capacity clients, and verify the same model row remains visible and focused.
