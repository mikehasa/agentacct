# Scenario

006 — Cost-conscious solo developer who primarily cares about estimated dollars.

# Verdict

Qualified pass. Removing the plan-percent/dollar mode fork fixes the current pane’s biggest failure for this user: a calibrated account can open on plan share and hide cost entirely.

# Findings

The merged pane should retain the current cost grammar and scan path: ranged estimated cost in the summary, estimated cost per day as the default trend, and per-client/per-model cost columns. Joining live headroom is useful context, but it must not push those dollar answers below a tall grid of quota cards.

“Est. cost” plus confidence text is directionally honest, yet a solo developer still needs to distinguish “estimated from pricing,” “client-reported,” and “partially priced” without hunting through a footer or disclosure. Missing priced usage must remain “no priced usage” or “unpriced,” never `$0`.

# Recommendation

Lead with the ranged dollar total and cost trend, then show a compact client row that pairs each client’s estimated spend with current headroom/reset. Keep provenance adjacent to the total, and place expanded limit-window mechanics and definitions behind disclosure.

# Test idea

Render a calibrated account with mixed complete, partial, and unpriced client costs. In ten seconds, ask the tester for total estimated spend, the costliest client/day, which values are incomplete, and whether any limit is near exhaustion.
