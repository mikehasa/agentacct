## Scenario

045 — Provider supplies an out-of-range negative percentage.

## Verdict

Fail closed.

## Findings

The meter visually clamps a negative value to its minimum fill while the row prints, for example, “-5%” in ordinary cobalt. This presents invalid provider data as a usable low-risk reading and can distort least-headroom ordering.

## Recommendation

Render a named “invalid provider percentage (-5%)” state with no quantitative fill. Exclude it from headroom sorting, retain its provenance in the disclosure, and never coerce it to 0%.

## Test idea

Inject -5%; assert no normal meter or low-risk color, explicit invalid copy, preserved raw value, and placement among unavailable readings.
