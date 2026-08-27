## Scenario

043 — Used percent crosses the 90-percent attention threshold.

## Verdict

Needs revision.

## Findings

The 90% notch is drawn, but values from 75% through 99.9% share the same amber color and generic “above notify threshold” copy. Crossing 90% therefore has no textual or accessibility state distinct from 75%.

## Recommendation

Keep amber below 100%, but add a “high attention · 90% threshold crossed” state in visible and spoken text. The notch should reinforce, not carry, the meaning.

## Test idea

Compare 89.9%, 90%, and 90.1%; assert the high-attention wording starts at 90% without prematurely using the limit-reached state.
