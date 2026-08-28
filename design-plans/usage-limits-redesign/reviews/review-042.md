## Scenario

042 — Used percent crosses the 75-percent attention threshold.

## Verdict

Pass, with boundary-copy cleanup.

## Findings

The current meter changes from cobalt to amber at 75%, retains the 75% notch, and adds “above notify threshold,” so the change does not depend on color alone. At exactly 75%, however, “above” is mathematically wrong.

## Recommendation

Use “at attention threshold” for exactly 75% and “above attention threshold” after it. Keep amber and the notch as secondary cues.

## Test idea

Render 74.9%, 75%, and 75.1%; verify color, notch, visible wording, and accessibility output on both sides of the boundary.
