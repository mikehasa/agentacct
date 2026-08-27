## Scenario

092 — Stale toggle context and announced count.

## Verdict

Needs accessible state feedback.

## Findings

Current toggle says only “Show stale accounts.” A separate sentence gives the hidden count, but the control has no identifier, count-bearing label/value, or result announcement. Moving it into About can make its scope even less obvious.

## Recommendation

Label it “Show N stale capacity readings,” expose on/off value and identifier, then announce “Showing X live and N stale readings” only after user activation.

## Test idea

Toggle three stale rows with VoiceOver and assert the control names three beforehand and announces the resulting visible counts once.
