## Scenario

098 — Deterministic snapshot controls.

## Verdict

Needs a dedicated merged-pane snapshot contract.

## Findings

Usage already substitutes chips for segmented pickers because `ImageRenderer` produces placeholders. Limits instead hides its stale toggle entirely. A merged pane adds range, measure, stale, and disclosure controls; silently omitting them would make review artifacts incomplete.

## Recommendation

Provide deterministic, truthful snapshot stand-ins for every platform control, including selected state and disclosure expansion, and render a fixed fixture clock.

## Test idea

Render twice in light/dark with range, measure, stale toggle, and About visible; assert pixel stability and no yellow placeholders or missing controls.
