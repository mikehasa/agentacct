## Scenario

071 — One paired range request fails.

## Verdict

Needs an explicit pane-local failure state.

## Findings

The store correctly awaits plan and usage as one tuple, so either failure preserves the previous range and both old payloads. However, `errorText` is not rendered by `UsagePane`; a selection can simply snap back with no explanation. The merged pane would appear broken despite honest atomicity.

## Recommendation

Retain prior data and range, then show an inline “Couldn’t load 30 days; still showing 7 days” status near the picker.

## Test idea

Fail only `/v1/plan?days=30`, then only `/usage/summary?days=30`; verify no mixed data and the visible fallback message names 7 days.
