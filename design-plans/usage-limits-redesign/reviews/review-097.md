## Scenario

097 — Menu-bar limit click-through.

## Verdict

Missing in current source and not concrete in the plan.

## Findings

Menu limit rows are static `HStack`s; only the general footer and session rows open the main window. There is no limit-specific action to inherit, despite the design’s stated click-through scope.

## Recommendation

Make each limit row, or a clearly labeled Limits section action, call the semantic `.limits` destination before opening the window. That redirect should land on merged Usage and clear stale Work selection.

## Test idea

Click a menu weekly-limit row with stale task/session IDs and assert the main window opens on Usage with the matching client row identifiable.
