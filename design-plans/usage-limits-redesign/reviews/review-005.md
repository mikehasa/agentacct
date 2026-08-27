## Scenario

005 — A team lead scans multiple clients to find the one needing attention.

## Verdict

Needs revision. Merging capacity and consumption is directionally right, but the candidate does not yet define a fast cross-client triage hierarchy.

## Findings

`LimitsPane` renders live limit cards in daemon order, while `UsageBreakdownTable` separately ranks clients by fresh tokens. A lead must mentally cross-reference two orderings. Within each card, urgency is buried at the window-row level; a risky second window may appear below a healthy first one. Threshold color and notches help locally, but there is no client-level “attention” label or concise worst-window summary. Clients with no limit reading are appended after all reporting clients, making unknown capacity easy to overlook.

## Recommendation

Make the merged by-client surface the primary triage list. Give each client a header summary such as “Attention · 92% used · resets today 14:20,” derived from its highest-risk live window, beside ranged tokens and estimated cost. Default-sort by explicit status groups: exceeded, attention (90%+, then 75%+), healthy, then “capacity unknown”; keep stale readings in a separately disclosed group. Do not rank unknown as healthy or infer urgency from spend alone.

## Test idea

Render six shuffled clients, including a risky second window, missing limits, and stale data. Assert deterministic status grouping and verify a snapshot exposes the sole attention client without expanding cards.
