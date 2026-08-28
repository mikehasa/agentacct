## Scenario

016 — Increased Contrast enabled.

## Verdict

Needs an explicit contrast variant.

## Findings

`Theme` resolves only light versus dark appearance. The candidate relies on hairlines, card borders, and narrow 75/90 notches, but custom colors will not automatically strengthen when macOS Increase Contrast is enabled.

## Recommendation

Read `accessibilityContrast` at the merged surface or token layer. In `.increased`, strengthen divider/border colors and widen meter notches without changing semantic thresholds. Keep percentage and status text so structure never depends on line visibility.

## Test idea

Render normal and increased-contrast variants in both schemes; assert visibly stronger boundaries and inspect 74/75/90% meters at 100% zoom.
