## Scenario

087 — Threshold meaning without color.

## Verdict

Partially met, not sufficient.

## Findings

The meter shows numeric percent and 75/90 notches, but text only says “above notify threshold” from 75% upward. It does not distinguish the 90% state or exhaustion, and unlabeled notches are not self-explanatory. Amber/coral therefore still carry essential severity.

## Recommendation

Add textual states such as normal, attention, critical, and exhausted beside each percent; keep notch explanation nearby.

## Test idea

Snapshot 74%, 75%, 90%, and 100% in grayscale and assert each threshold transition remains understandable from text and shape.
