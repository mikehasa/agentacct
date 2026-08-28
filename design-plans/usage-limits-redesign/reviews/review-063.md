## Scenario

063 — Client and model names contain Unicode and emoji.

## Verdict

Supported in principle; verification is required.

## Findings

SwiftUI `Text` can render these labels, but the candidate uses display names for joining, sorting, and likely row identity. Composed characters, emoji sequences, or canonically equivalent strings can expose unstable identity and truncation bugs.

## Recommendation

Keep display text lossless, use payload/domain identity where available, and apply Unicode-aware comparison only for tie-breaking. Never normalize what the user sees into an ASCII key.

## Test idea

Mix CJK, Arabic, accents in composed/decomposed forms, and ZWJ emoji; verify joins, stable IDs, sorting, truncation, and complete accessibility labels.
