## Scenario

041 — Used percent is exactly zero.

## Verdict

Needs a meter correction.

## Findings

`LimitMeter` clamps the fill with `max(4, …)`, so 0% still paints a four-point accent segment. The adjacent “0%” is truthful, but the graphic implies nonzero use. In a compact ledger, the visual can dominate the text.

## Recommendation

Render an empty track at exactly 0%; preserve the text and accessibility label. Reserve a minimum visible fill only for positive values too small to see.

## Test idea

Snapshot 0%, 0.1%, and 1%. Assert zero has no fill, positive values remain perceivable, and VoiceOver announces “0 percent used.”
