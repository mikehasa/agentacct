## Scenario

009 — A user assumes displayed cost is a provider invoice unless corrected.

## Verdict

Needs revision. The merged page preserves numeric provenance but does not reliably correct the invoice assumption.

## Findings

“Est. cost” plus `≈$`/`~$` helps for estimates, yet complete values use a bare `$`. The nearest qualifier can read “client-reported” or “provider billed”; neither says “not an invoice,” and “provider billed” may strengthen the misconception. The basis footer repeats confidence only after the summary, chart, and breakdowns. Chart and table values carry no adjacent source explanation, while placement beside provider quota data can make all figures feel equally authoritative. A scanning user can reasonably treat the KPI as charges due.

## Recommendation

Place an always-visible sentence beneath the cost KPI or section title: “Usage cost from recorded client/provider data—not a provider invoice or balance due. Verify charges with your provider.” Pair it with a compact legend for `$` reported-complete, `≈$` estimated, `~$` partial, and `—` unpriced. Keep per-value confidence accessible; do not rely on a footer or disclosure to establish the core caveat.

## Test idea

Snapshot each confidence state and assert the invoice caveat remains visible beside the first dollar value. In a five-second comprehension test, ask users what the amount represents; pass only if they distinguish usage reporting from an invoice.
