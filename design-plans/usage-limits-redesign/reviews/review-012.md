## Scenario

012 — Large desktop window at 1600 by 1000.

## Verdict

Needs revision. Both panes remain legible, but their shared large-screen behavior wastes space and looks pinned left rather than intentionally composed.

## Findings

`UsagePane` and `LimitsPane` both add a 28-point gutter, cap the outer stack at `1172 + gutter * 2`, then align it leading inside the window. At 1600 points, this leaves roughly 372 points empty on the right versus 28 on the left. The asymmetry is conspicuous. The cap also holds Limits' adaptive 420-point grid to two roughly 574-point columns, although three roughly 499-point columns fit in the available inner width. Usage's tables and chart benefit from a readable maximum, so globally removing the cap would over-stretch them.

## Recommendation

Use a shared responsive container rule: center Usage at its current readable maximum, while allowing Limits a wider large-window breakpoint capped at three columns. Keep each header aligned with its pane content.

## Test idea

Render both panes at exactly 1600 by 1000. Assert equal outer gutters for Usage and three unclipped 420-point-or-wider cards for Limits; snapshot one-, two-, and three-card states.
