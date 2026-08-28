## Scenario

020 — Long localized labels and right-to-left reading order.

## Verdict

Not yet specified adequately.

## Findings

Current panes use fixed numeric columns, English literals, abbreviated reset text, and `String(format:)` percentages. A three-lane ledger can overflow in expansion languages, while raw custom-window labels and mixed Latin numerals complicate RTL ordering.

## Recommendation

Use string-catalog messages, locale-aware percent/date formatting, leading/trailing alignment, and flexible lane widths. Preserve semantic order—client, capacity, consumption—through accessibility ordering even when the visual layout mirrors.

## Test idea

Render German pseudolocalization and Arabic RTL with a long client and reset label; verify no truncation and confirm VoiceOver reads the three facts coherently.
