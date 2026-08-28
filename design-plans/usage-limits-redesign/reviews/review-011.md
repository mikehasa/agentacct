# Scenario

011 — Minimum supported window at 960 by 560.

# Verdict

Needs revision.

# Findings

Width is viable: the Usage controls and fixed-width table columns fit inside 28-point gutters. Height is the blocker. Below the 46-point top bar, only about 513 points remain. Usage already stacks a header, summary, optional chart, and two tables; Limits adds cards, calibration, and definitions. When merged vertically, Limits starts below the first viewport in populated states. Live `ScrollBox` hides scroll indicators, so nothing persistently signals that the second domain exists.

# Recommendation

Keep one top-level destination, but switch its body with a sticky “Usage / Limits” control near the existing range and mode controls. Preserve each selection’s scroll position and show a scrollbar on overflow. This keeps both domains discoverable without compressing tables or meters.

# Test idea

Render populated fixtures at exactly 960 by 560. Verify controls do not truncate, either domain is reachable without scrolling the other, and each body can reach its last row by mouse, trackpad, and keyboard.
