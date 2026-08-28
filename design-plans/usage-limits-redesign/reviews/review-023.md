## Scenario

023 — Limits exist but the usage summary has not loaded.

## Verdict

The candidate’s row membership needs broadening.

## Findings

“A row for every recording client” can accidentally omit a limit-only client when `dashboard.usage` is nil. Current limit cards remain useful independently, so capacity should not disappear because the ranged lane is unavailable.

## Recommendation

Join the union of live-limit and usage client identities. Show the provider window normally and name the right lane “Usage not loaded”; never substitute zero tokens, zero sessions, or $0. Surface a usage-specific loading/error state.

## Test idea

Load two live limit clients with `usage == nil`; verify both rows and meters render while every consumption field is explicitly unavailable.
