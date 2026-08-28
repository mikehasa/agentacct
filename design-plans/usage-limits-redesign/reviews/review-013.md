## Scenario

013 — Narrow navigation fit after removing the separate Limits tab.

## Verdict

Conditional pass.

## Findings

The merge removes one destination, but naming the remaining tab “Usage & limits” gives back some width. The current `TopBar` reserves 76 points after `BrandLockup`, then `ViewThatFits` collapses every label to icons when the full tab strip cannot fit. If the candidate only deletes `.limits` while retaining that breakpoint, the 960-point window may still become icon-only when setup, freshness, and refresh controls are present. That wastes the merge’s clearest navigation benefit and makes the newly combined destination less discoverable.

## Recommendation

Keep “Usage & limits” visible at 960 points. Rebalance fixed brand padding or status controls, or add a four-tab layout threshold before using the icon-only fallback. Preserve a concise accessibility label if visual shortening is unavoidable.

## Test idea

Render `TopBar` at 960 points in its worst-case chrome state (setup shown, freshness text, refresh or progress) and assert four labeled tabs fit without clipping, truncation, overlap, or fallback; repeat with the merged tab selected.
