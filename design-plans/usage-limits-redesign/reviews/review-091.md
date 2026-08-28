## Scenario

091 — Visible focus rings on selected treatments.

## Verdict

Requires live verification.

## Findings

Navigation uses a custom 2pt accent focus stroke over the selected card, which is promising. Segmented controls and the proposed disclosure rely on platform focus rendering; the design does not define their focused appearance against cobalt selection, card fill, or dark mode.

## Recommendation

Specify a high-contrast focus outline independent of selection fill and test every custom control in light, dark, and Increased Contrast modes.

## Test idea

Keyboard-focus the selected Usage tab, active 7d segment, stale toggle, and About disclosure; capture both appearances and verify an unambiguous ring.
