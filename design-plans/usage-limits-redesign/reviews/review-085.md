## Scenario

085 — Plan share includes unusable timestamps.

## Verdict

Current truth handling should be retained.

## Findings

`unknownTimePct` is shown as an additive estimate explicitly outside the daily bars. That prevents the chart from pretending the amount belongs to a known date. Folding calibration into a disclosure must not reduce this to a footnote detached from totals.

## Recommendation

Keep the unknown-time amount beside the plan summary and state that daily bars exclude it; include it in the accessible summary.

## Test idea

Use 8% dated plus 2% unknown-time share and verify the bars represent 8%, while text and VoiceOver account for the separate 2%.
