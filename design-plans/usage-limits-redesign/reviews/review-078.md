## Scenario

078 — Rolling meter can never calibrate.

## Verdict

Pass if the incompatibility remains explicit.

## Findings

Current copy correctly says weekly plan percent is undefined for that client’s rolling meter. The merged design moves calibration into details, which is appropriate, but generic “Learning limit history” in a capacity row would falsely promise eventual completion.

## Recommendation

Use a terminal state such as “Weekly plan share unavailable — rolling meter is incompatible,” while still showing the provider’s live rolling window and recorded usage.

## Test idea

Render `calibrationState = never` with a valid 5-hour window and verify the live meter remains while no learning/progress language appears.
