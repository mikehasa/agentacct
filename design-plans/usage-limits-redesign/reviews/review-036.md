## Scenario

036 — Window has no reset timestamp.

## Verdict

Existing truth handling should be preserved.

## Findings

The percent and meter remain valid even when timing is absent. Current copy correctly says “Reset time unreported,” whereas a countdown, guessed cadence, or hidden field would imply unavailable provider knowledge.

## Recommendation

Keep the meter and used percentage, render the named reset absence in the same location as normal reset text, and continue sorting by headroom. Do not downgrade the entire limit row to unavailable.

## Test idea

Render weekly windows at 40% and 92% with nil resets; verify meters and attention ordering work and both rows announce reset time as unreported.
