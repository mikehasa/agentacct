## Scenario

100 — Final navigation, docs, tests, stale-code, and scope regression.

## Verdict

Stop release until the merge is complete across repository surfaces.

## Findings

Current source still has five panes, `MainPane.limits`, `LimitsPane`, tests expecting `.limits`, and README copy/screenshots describing separate Usage and Limits. Existing visual baselines do not gate either pane. These are required migration work, not optional cleanup.

## Recommendation

Audit the whole diff for four tabs, semantic redirects, orphan code, docs/assets, focused state tests, merged snapshots, and no daemon/schema changes. Run unit, snapshot, release-build, and live keyboard/VoiceOver smoke checks.

## Test idea

Search for stale Limits navigation/copy, run the full suite and build, then inspect the merged pane at 960pt in light/dark.
