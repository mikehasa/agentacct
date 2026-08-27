## Scenario

007 — A subscription user opens Usage & limits primarily to learn remaining quota and when it resets.

## Verdict

Needs revision: merging the panes removes navigation friction, but the current quota presentation does not answer “can I keep working?” at a glance.

## Findings

`LimitsPane` leads each window with percent used, forcing the user to subtract from 100 to infer headroom. Reset time is small, muted, and right-aligned beneath the meter, so the two facts this user needs are visually secondary. With ranged usage now sharing the pane, the 7d/30d/90d control could also be mistaken for changing the provider quota window. Plan-share calibration is a separate estimate and must not compete with provider-reported capacity.

## Recommendation

Lead the merged pane with a clearly labeled “Current capacity” section, independent of the selected usage range. For every live window, pair an explicit value such as “23% remaining” with “Resets today 12:40”; retain percent used as secondary detail and keep the provider’s window name visible. Surface the most constrained fresh window first, while keeping other windows in the same client card. Label ranged consumption “Usage over last N days” and place calibration/basis details behind disclosure.

## Test idea

Seed one client with 77% used in a 5-hour window resetting today and 42% used weekly resetting Monday. In a 10-second test, verify the user identifies 23% headroom, today’s reset, and that switching 7d to 30d does not alter either live quota fact.
