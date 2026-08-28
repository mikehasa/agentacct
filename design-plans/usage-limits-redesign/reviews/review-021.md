## Scenario

021 — No recorded usage and no client limit readings.

## Verdict

Requires one unified empty state.

## Findings

With neither source populated, the signature ledger has no rows. Rendering an empty Capacity section followed by absent totals and empty breakdowns would look broken and multiply the same fact.

## Recommendation

Show a single primary state: “No usage or provider limits recorded yet,” explain that recording data creates this view, and offer the existing setup entry point when available. Suppress chart, totals, and model sections; do not render zeros.

## Test idea

Use connected empty payloads for both lanes and assert one empty message, no fabricated meter/KPI, and a setup action only when setup is supported.
