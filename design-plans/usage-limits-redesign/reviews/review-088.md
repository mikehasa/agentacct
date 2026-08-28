## Scenario

088 — Complete VoiceOver limit summaries.

## Verdict

Fails in the reusable current meter.

## Findings

`LimitMeter` exposes only “N percent used.” It omits client, provider-window name, reset time, stale status, and threshold state. A merged row containing multiple windows would force VoiceOver users to reconstruct context from separate elements.

## Recommendation

Make each capacity window one combined accessibility element: client, window/span, percent or unreported state, severity, reset, and freshness. Hide decorative fill/notches.

## Test idea

Inspect the accessibility tree for a stale weekly 92% window resetting Friday; assert one concise label contains every decision fact once.
