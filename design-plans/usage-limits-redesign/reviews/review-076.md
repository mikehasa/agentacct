## Scenario

076 — No client supports calibrated weekly plan share.

## Verdict

The merge resolves the current dead-end well.

## Findings

Today, the plan mode can collapse to “No calibrated plan estimate yet.” The candidate removes that page-level fork, so provider windows and recorded consumption remain primary even when plan share is impossible. Calibration belongs in the disclosure as supporting evidence, not a missing main view.

## Recommendation

Show a concise “Weekly plan share unavailable for these clients” disclosure row while leaving every capacity/usage row intact.

## Test idea

Use only `never` and absent calibration states; verify no empty hero appears and all client usage remains actionable.
