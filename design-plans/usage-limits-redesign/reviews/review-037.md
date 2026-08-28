## Scenario

037 — Reset happens later today.

## Verdict

Current absolute phrasing is appropriate.

## Findings

`resetsAtText` uses the local calendar and produces “Resets today HH:mm,” which is more actionable and stable than a countdown. The merged compact row must not truncate that timing behind the consumption lane.

## Recommendation

Keep “Resets today” plus locale-formatted time visible beside the provider window. Give the combined accessibility label the full date and time so “today” retains context.

## Test idea

Freeze the clock morning and evening with same-day resets, including near midnight; verify local time, today classification, layout, and accessibility text.
