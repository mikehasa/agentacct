## Scenario

089 — Chart detail without hover.

## Verdict

Needs interaction redesign.

## Findings

Daily usage bars have accessibility labels but are not keyboard focusable; their visible tooltip is hover-only. `PlanDailyChart` is weaker: its bars have neither focus nor accessibility labels. The merged pane preserves the usage chart, so this remains a release blocker.

## Recommendation

Adopt the dashboard chart’s focus/pin pattern or a keyboard-accessible data table alternative. Give every day date-and-value semantics.

## Test idea

Using keyboard and VoiceOver only, traverse all days, reveal the same detail as hover, and pin/clear a value without moving the pointer.
