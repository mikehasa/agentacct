## Scenario

018 — Reduce Motion enabled during navigation and range changes.

## Verdict

Mostly sound, with a ledger-reordering risk.

## Findings

Pane crossfades and current chart hover animations already honor `accessibilityReduceMotion`; range loading itself has no required animation. The new least-headroom ordering could still cause rows to slide or crossfade when fresh data lands.

## Recommendation

Gate every range transition and ledger reorder on Reduce Motion. When enabled, replace content in place, preserve scroll/focus, and avoid matched geometry or animated numeric interpolation.

## Test idea

Enable Reduce Motion, navigate into Usage, switch 7d→30d, then refresh changing row order; verify there is no animation and keyboard focus remains stable.
