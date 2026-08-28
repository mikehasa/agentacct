## Scenario

081 — Older payload omits calibration state.

## Verdict

Needs a compatibility state.

## Findings

The decoder tolerates a missing `calibrationState`, but current plan filtering silently treats it as not calibrated while Limits emits an “unknown” chip. In the merged pane, absence must not be interpreted as unsupported, calibrating, or zero share.

## Recommendation

Render “Calibration status not reported by this daemon” and any available basis separately. Do not expose plan percentages unless the payload explicitly establishes calibration.

## Test idea

Decode a client with no calibration key but with usage and limits; assert those facts render and the calibration detail names the missing status.
