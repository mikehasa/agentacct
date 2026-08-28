## Scenario

096 — Legacy `.limits` navigation with stale selections.

## Verdict

Preserve the existing clearing behavior exactly.

## Findings

`AppSelection.open(.limits)` currently nils both `taskId` and `sessionId` before switching panes. Simply replacing direct `.limits` assignments elsewhere would risk leaving deep Work selection latent and resurrecting it later.

## Recommendation

Keep `.limits` as a semantic `DashboardDestination`, map it internally to `.usage`, and centralize all selection clearing there. Remove only `MainPane.limits`.

## Test idea

Exercise legacy limit routing from dashboard and menu with both stale IDs set; then visit Work and confirm no old task or session reopens.
