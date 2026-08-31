# Session and steps readability research

Audience: agentacct developers, operators, and reviewers

Decision date: 2026-08-28
Decision: choose an issue-first verification ledger with progressive history disclosure

## Executive answer

The current Session and steps surface has the right data and the right high-level hierarchy, but its check presentation inverts attention. A completed/claimed step and a long sequence of nearly identical passing rows dominate the page while a current failure can be buried in the middle. Every row forces result, type, summary, exit code, provenance, and supersession into one horizontal line, and the expanded view truncates summaries to two lines despite promising full detail.

The selected design keeps `session → step → checks`, makes active failed/error results the first group, bounds both issue and ordinary previews with exact hidden counts, and moves superseded records into explicit history. Each check becomes a two-level ledger row: visible result word and glyph plus the full summary, then wrapping machine/provenance metadata. No API or daemon change is needed.

## Scope and assumptions

- Product: native macOS SwiftUI app, minimum macOS 14.
- User job: decide what needs attention, then inspect enough evidence to trust or challenge the work.
- In scope: Session and step disclosure headers, current/history check semantics, check-row layout, empty/loading/failure states, accessibility semantics, deterministic focused snapshots, and presentation tests.
- Out of scope: daemon schema changes, global navigation, receipt decision rules, action-digest redesign, localization infrastructure, and a new searchable audit-log workbench.
- Initial diagnosis base: `origin/main` at `89c34f851a5ba977822fda3f63f03049a09641b1`.
- Final integration base: `origin/main` at `6679061606de628ca1f625f0871a18268c127860`.
  That base includes the separate receipt-level Checks ledger from PR #166;
  this work remains focused on the nested checks inside Session and steps.

## Research method and scenario census

Three specialist review lanes and the coordinating implementation pass simulated exactly 100 distinct real-life scenarios:

- [Scenarios 1–34](scenarios-01-34.md): developer/operator/reviewer workflows.
- [Scenarios 35–67](scenarios-35-67.md): accessibility, typography, keyboard, RTL, and low-vision workflows.
- [Scenarios 68–100](scenarios-68-100.md): data truth, legacy/malformed states, density, ordering, performance, and verification.

This is a 100-scenario structured simulation, not 100 independent agents or an empirical usability study. The environment provided three specialist agent slots; each lane used the same inspected repository state and a distinct scenario family.

## Verified cause

1. `/v1/session` already projects complete step/check records with result, source type, supersession state, timestamp, exit code, resolution, and safe artifact fields. The data boundary is sufficient.
2. `CheckRow` renders all semantic fields in one `HStack`; trailing metadata removes width from the summary.
3. `CheckRow` applies `lineLimit(2)` inside an expanded detail whose source comment promises no truncation.
4. The step header reports raw record count, combining current evidence and superseded history.
5. Result is primarily a small glyph; most text has the same muted weight, while repeated provenance chips become the strongest rhythm.
6. Session/step buttons rely on synthesized accessibility speech and expose no explicit expanded/collapsed value or target hint.
7. Existing full-page Work references stop above Session and steps, so the failure has no visual-regression coverage.

## Options considered

### A. Restyle the existing flat list

Give summaries more width, reduce chip padding, and add separators. This is the smallest visual diff, but it leaves active failures buried, current and historical checks conflated, and long ledgers unbounded. Rejected because it treats the symptom only.

### B. Issue-first verification ledger — selected

- Preserve server order within `Needs attention`, ordinary current checks, and history.
- Show active failed/error checks before routine results, with a bounded initial issue preview and exact remaining count.
- Budget the initial ordinary preview so a typical expanded step stays scannable; show the exact remaining count.
- Keep superseded records reachable behind a separately named History disclosure.
- Use full selectable summary text and a wrapping metadata grammar (`exit · source · time`) instead of repeated pills.
- Keep source/result semantics conservative: unknown source remains unknown; an agent-reported pass never receives independent green styling.

This option changes the information hierarchy without inventing new data or a large control surface.

### C. Searchable/filterable check workbench

Add columns, filters, sort, and pagination. This could suit receipts with thousands of records, but it would create a second product inside every step, add keyboard/focus complexity, and make small one-check steps unnecessarily heavy. Rejected for this focused PR; the bounded ledger leaves room for a future dedicated audit view if real usage proves it necessary.

## Design system and signature

- Palette: reuse agentacct semantic ink, muted, green, coral, amber, card, and hairline tokens; add no decorative colors.
- Type: full evidence summaries use the 14-point body role; result/type and provenance use existing 12-point caption/data roles. Weight and position, not new typography, carry hierarchy.
- Layout: the wide form stays compact; constrained forms stack metadata before truncating semantic text.
- Signature: a quiet evidence ledger where a result word/glyph anchors the left edge, the full proof statement occupies the reading column, and provenance becomes a consistent secondary sentence rather than a wall of pills.
- Deliberate risk: remove high-emphasis provenance chips from every current row. Provenance remains visible as text and in the accessibility summary, but no longer competes with what the check actually says.

## Invariants and acceptance criteria

- Active `failed` and `error` results are never classified as history unless `supersession_state == superseded`.
- `unconfirmed` remains current and actionable.
- Current and historical counts are never merged into an ambiguous raw total.
- The active-issue count and first eight issues remain in the initial view; additional issues and ordinary current checks use exact `Show N more` affordances.
- Expanded summaries are never line-limited and remain selectable.
- Result meaning uses text and shape as well as color.
- Agent-reported passes never use independent green; missing/future source values render as `source unknown`.
- Duplicate/missing event IDs receive stable occurrence identity at the view boundary.
- Result/exit-code contradictions remain verbatim and receive a neutral inconsistency cue.
- Session and step disclosures remain native buttons with explicit label, value, and hint.
- Empty, loading, and load-failure states are distinct; failure offers Retry.
- Focused light/dark references cover hierarchy, 25-check density, expanded current/history states, compact width, RTL stress, and accessibility5 compact/RTL reflow on the canonical renderer.

## Evidence gap matrix

| Claim | Primary evidence | Confidence | Remaining gap |
| --- | --- | --- | --- |
| Essential information should remain visible while details disclose progressively. | Apple HIG, Disclosure controls | High | Apple does not prescribe a check-preview threshold. |
| Long/localized content needs a vertical alternative when horizontal layout no longer fits. | Apple, Composing custom layouts with SwiftUI; `ViewThatFits` | High | Exact breakpoint must be verified in this app. |
| Typography must express hierarchy, minimize truncation, and stay legible at macOS sizes. | Apple HIG, Typography | High | Live macOS Zoom and user text settings remain manual checks. |
| Accessible grouping, labels, values, hints, and headings improve VoiceOver navigation. | Apple, SwiftUI Accessibility modifiers and Accessible descriptions | High | Exact speech and focus retention require live VoiceOver verification. |
| Color cannot be the only status carrier, and focus order must preserve meaning. | W3C WCAG 2.2 Understanding 1.4.1 and 2.4.3 | High as a heuristic | WCAG is web-normative, not native macOS certification. |
| The API already contains the required check truth. | `v1_sessions.py` and `work_ledger.py` in this repository | High | Live legacy-daemon payloads still need smoke coverage. |

## Claim-to-source ledger

- **Disclosure controls**, Apple Human Interface Guidelines, current page accessed 2026-08-28. https://developer.apple.com/design/human-interface-guidelines/disclosure-controls
- **Typography**, Apple Human Interface Guidelines, updated 2025-12-16; accessed 2026-08-28. https://developer.apple.com/design/human-interface-guidelines/typography
- **Accessibility**, Apple Human Interface Guidelines, updated 2025-06-09; accessed 2026-08-28. https://developer.apple.com/design/human-interface-guidelines/accessibility/
- **Lists and tables**, Apple Human Interface Guidelines, current page accessed 2026-08-28. https://developer.apple.com/design/human-interface-guidelines/lists-and-tables
- **ViewThatFits**, Apple Developer Documentation, current page accessed 2026-08-28. https://developer.apple.com/documentation/swiftui/viewthatfits
- **Composing custom layouts with SwiftUI**, Apple Developer Documentation, current page accessed 2026-08-28. https://developer.apple.com/documentation/swiftui/composing-custom-layouts-with-swiftui
- **Accessibility modifiers** and **Accessible descriptions**, Apple Developer Documentation, current pages accessed 2026-08-28. https://developer.apple.com/documentation/SwiftUI/View-Accessibility and https://developer.apple.com/documentation/swiftui/accessible-descriptions
- **Use of Color, SC 1.4.1**, W3C WAI WCAG 2.2 Understanding, updated 2025-09-16. https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html
- **Focus Order, SC 2.4.3**, W3C WAI WCAG 2.2 Understanding, current page accessed 2026-08-28. https://www.w3.org/WAI/WCAG22/Understanding/focus-order.html
- **Reflow, SC 1.4.10**, W3C WAI WCAG 2.2 Understanding, current page accessed 2026-08-28. https://www.w3.org/WAI/WCAG22/Understanding/reflow.html

## Limitations and stopping rule

No source prescribes issue-first ordering or an eight-row preview; those are product-design inferences tested against the observed 25-check failure and the 100-scenario matrix. WCAG guidance is used as a conservative native-app heuristic. Static review and snapshots cannot prove live VoiceOver speech, Switch Control scanning, keyboard focus retention, or real legacy-daemon behavior.

Research stopped when the material claims had Apple/W3C or repository-primary support, the three lanes converged on the same hierarchy, and further broad searching was unlikely to change the selected option. Implementation verification must close the remaining app-specific gaps.
