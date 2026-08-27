## Scenario

084 — 30d/90d model share exceeds 100% of a weekly plan.

## Verdict

Pass with the existing window qualifier preserved.

## Findings

Current model-share copy says “estimated · last Nd,” correctly framing the percentage as multi-week accumulation rather than remaining quota. The merged design risks burying that distinction under a generic About heading while the nearby provider meter is capped at 100%.

## Recommendation

Label the table “accumulated weekly-plan equivalents over 30/90 days” and never reuse the capped capacity-meter visual for these shares.

## Test idea

Render 135% at 30d and assert the full value, accumulation window, and estimate marker remain visible.
