## Scenario

027 — Incompatible daemon schema.

## Verdict

Current fallback is too generic for the merged pane.

## Findings

`GlanceState` carries a useful version/schema explanation, but `LimitsPane` collapses `.incompatible` and `.disconnected` into “Daemon not connected.” That hides the corrective action and misdescribes a reachable daemon.

## Recommendation

Give Capacity an explicit “Incompatible daemon” state showing the provided version/schema message and update guidance. Retain any independently valid usage as dated data, without implying the incompatible capacity payload was parsed.

## Test idea

Inject `.incompatible` with cached usage; verify the exact compatibility detail is visible, meters are absent, and the usage range remains readable.
