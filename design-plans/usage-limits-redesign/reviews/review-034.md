## Scenario

034 — Provider reports an unfamiliar custom window kind.

## Verdict

Accept only with lossless fallback labeling.

## Findings

Current `windowName` passes unknown kinds through, which preserves truth, and can add the reported minute span. The plan’s rolling/fixed definitions must not classify a custom kind without a payload fact.

## Recommendation

Display a safe human fallback such as “Provider window: monthly_beta” plus reported span, percent, and reset. Sort normally when percent exists, but do not rename it Weekly or infer rolling/fixed behavior.

## Test idea

Render kind `monthly_beta` with a 43,200-minute span; verify the raw kind remains discoverable, all facts render, and no known-window label is substituted.
