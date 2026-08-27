## Scenario

046 — Fresh and stale readings coexist for the same client.

## Verdict

The merged join needs an explicit precedence rule.

## Findings

The current pane filters stale entries before display, but a client-keyed merged row can accidentally let input order decide which reading survives. Mixing stale windows into the fresh meter would fabricate a single current capacity state.

## Recommendation

Use only fresh readings for “Capacity now.” Keep stale siblings in “About these numbers,” labeled with age and account context; never merge their percentages into fresh windows.

## Test idea

Feed fresh and stale entries in both orders. Assert the same fresh meter leads, one stale sibling is disclosed, and usage joins exactly once.
