## Scenario

062 — One client has a very long generated identifier.

## Verdict

Pass only with the existing truncation discipline.

## Findings

The usage table already middle-truncates a name to one line, preserving both identifier ends. Limit cards do not. In the merged 960-point ledger, an unbounded identifier could displace capacity and consumption lanes.

## Recommendation

Give the name column a bounded width, use middle truncation, and expose the complete identifier through accessibility and hover/help text. Do not shrink numeric columns.

## Test idea

Render a 160-character identifier at minimum width; verify one complete row, visible prefix/suffix, unchanged metrics, and full spoken name.
