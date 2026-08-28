# Session and steps readability: scenarios 01–34

> Read-only specialist simulation lane: 34 realistic role-play scenarios, **not 34 independent agents**. Findings are based on the supplied dense-receipt screenshot, the `SessionDrillRow → StepCard → CheckRow` implementation, the Work snapshot fixture, and primary Apple/W3C guidance.

Priority: **P0** means truth, accessibility, recovery, or core comprehension; **P1** means high-value review efficiency and resilience.

## Scenario ledger

| # | Role / context | Goal | Current failure | Design requirement | Priority |
|---|---|---|---|---|---|
| 01 | New adopter opening a first receipt | Decide whether work is genuinely done | “completed” and “claimed” dominate while a failing check is buried below many passes | Put current failing/error count in the step header and keep unresolved failures visible before history | P0 |
| 02 | Maintainer reviewing a routine green change with 25 checks | Confirm broad coverage quickly | Repeated `test`, `exit 0`, and `agent-reported` labels drown out suite meaning | Show a concise result summary first; demote repeated metadata to a secondary line | P1 |
| 03 | PR reviewer with five minutes | Find the one result needing review | Every check has nearly equal visual weight | Create a clearly labeled **Needs attention** group before settled history | P0 |
| 04 | Incident responder during a regression | Identify the newest live failure and its scope | Rows expose no timestamp and failures remain in an undifferentiated stream | Show failure/error time and scope; order live issues newest-first without losing history | P0 |
| 05 | Release manager at a go/no-go gate | Distinguish failed, skipped, and superseded results | The count only says “25 checks”; result composition is invisible | Header needs passed/failed/skipped/superseded breakdown | P0 |
| 06 | Engineering lead assessing verification quality | Determine whether evidence is independent | Provenance is repeated but not summarized; “claimed” conflicts cognitively with checkmarks | Summarize source mix, retain per-row provenance in muted metadata, and preserve claim ≠ proof wording | P0 |
| 07 | Compliance reviewer auditing an approval | Trace who asserted each result | Source chips are readable only by laboriously scanning every row | Keep provenance programmatically and visually attached to each check, with aggregate source counts | P0 |
| 08 | CI engineer comparing hook versus CI evidence | Locate externally observed checks | All passing marks look similar at scan speed | Pair source text with an independence-aware symbol/label; never rely on tint alone | P0 |
| 09 | Security reviewer encountering redacted commands | Understand what was checked without exposing arguments | The compact row cannot surface command-redaction or artifact context | Provide optional evidence detail for redaction, artifacts, files, and scope | P1 |
| 10 | VoiceOver user | Navigate session → step → checks in a meaningful order | Session and step disclosures lack explicit labels/values; decorative content risks noisy announcements | Add combined label, expanded/collapsed value, result counts, hints, headings, and hidden decorative glyphs | P0 |
| 11 | Keyboard-only reviewer | Expand the relevant level without traversing noise | Deep disclosure controls offer little focus context | Preserve session → step → history focus order and name every disclosure’s target/state | P0 |
| 12 | Low-vision user with large text | Read full summaries | One horizontal `HStack` fights text expansion and clips summaries at two lines | Use `ViewThatFits` or a vertical fallback; expanded content must not truncate | P0 |
| 13 | Color-vision-deficient reviewer | Distinguish pass/fail/skipped | Result meaning leans on green/coral/amber | Always pair tint with distinct symbol and spoken/visible result text | P0 |
| 14 | User resizing the macOS window narrow | Keep check meaning intact | Trailing exit/source pills take width from the summary | Move metadata below the summary at constrained widths | P0 |
| 15 | Reviewer reading a long human-authored verification summary | Understand exact limitations | `CheckRow` applies `.lineLimit(2)` despite the expanded view promising no truncation | Render the full selectable summary in expanded history | P0 |
| 16 | User viewing localized or verbose labels | Avoid metadata collisions | Fixed horizontal composition assumes short English tokens | Allow flexible wrapping and keep title/summary ahead of secondary metadata | P1 |
| 17 | Small receipt with one successful check | Verify without extra ceremony | The hierarchy feels heavier than the data | Keep one-check presentation compact and visible without another unnecessary expansion | P1 |
| 18 | Planning step with zero checks | Understand why evidence is claimed | Absence is implicit after the evidence explanation | Show a named “No machine checks recorded” state | P1 |
| 19 | Large receipt with 100+ checks | Find signal without scrolling an enormous card | Expanding a step reveals the entire raw ledger immediately | Show issue summary plus a bounded preview and an explicit “Show all N checks” control | P0 |
| 20 | Maintainer with repeated reruns of the same suite | Identify the latest authoritative run | Same-type rows appear indistinguishable | Surface time, identity/scope, and supersession; group or label rerun history | P0 |
| 21 | Reviewer of fail-then-pass recovery | Verify the failure was actually superseded | “superseded” is a trailing chip detached from the resolution | Pair the superseded result with resolving state/summary and exclude it from live-issue counts | P0 |
| 22 | TDD reviewer expecting an intentional red phase | Distinguish expected historical failure from current failure | A superseded expected-red row visually competes with live failures | Separate **Current issues** from **Earlier / superseded checks** | P0 |
| 23 | Build engineer reviewing skipped checks | Determine whether skipping is acceptable | Skipped is a chevron-like mark with no aggregate visibility | Count skipped checks and retain their reason in full | P1 |
| 24 | Operator seeing `error` or missing exit code | Distinguish infrastructure error from test failure | `error` and `failed` share presentation; absent exit is unexplained | Preserve the exact result word and handle missing exit/source explicitly | P0 |
| 25 | Reviewer comparing agent, hook, CI, and provider sources | Judge trust boundaries | Repeated pills are noisy, but removing them would hide trust | Use a consistent grammar: `source · result · exit · time`; summarize source mix above | P0 |
| 26 | Artifact reviewer | Open or copy the supporting artifact/file | Step file claims are far below checks; check-specific artifacts are absent | Keep check artifacts/files attached to their check and selectable/actionable where safe | P1 |
| 27 | User on a slow daemon connection | Know whether session steps are still loading | Loading appears only as small muted text | Keep progress adjacent to the expanded session header and announce loading state | P1 |
| 28 | User after a transient session-load failure | Recover without collapsing and reopening | Error text says “couldn’t load” but offers no retry | Add a clear Retry action and preserve session context | P0 |
| 29 | Lead reviewing root plus many subagents | Know which checks belong to which actor | Flat sibling sessions plus similar titles make attribution laborious | Add per-session step/check/issue counts and retain distinguishing role/title/id | P1 |
| 30 | Reviewer following a continuation session | Understand chronological task handoff | Primary/continuation labels lack useful aggregate context | Label each group with sessions, steps, issues, and recency | P1 |
| 31 | User observing an in-progress step | Separate current activity from settled results | Lifecycle, evidence grade, and checks share one overloaded header rail | Use a two-line header separating lifecycle/recency from evidence/result summary | P0 |
| 32 | Blocked-task owner | Find blocker and next action immediately | Blocker and next step appear after all checks | Put blocker and next action before check history | P0 |
| 33 | User connected to an older daemon with missing fields | Still understand the receipt honestly | Fallbacks can produce vague “check,” no time, no grade, or no source | Use explicit “not recorded” vocabulary and avoid fabricated zero/unknown confidence | P0 |
| 34 | Handoff reviewer copying evidence into a PR | Capture a concise but truthful status | Relevant summaries are fragmented across header, prose, and 25 rows | Make step summary and full check text selectable; expose a stable readable order | P1 |

## Ranked findings

1. **P0 — Truth is visually inverted.** A current failure can sit deep beneath a “completed / claimed / 25 checks” header. The first glance must expose current issues and result composition.
2. **P0 — The expanded-detail contract is broken.** `StepCard` says expanded content is untruncated, but `CheckRow` applies `.lineLimit(2)` to the check summary.
3. **P0 — Secondary metadata owns primary real estate.** `CheckRow` places summary, exit, provenance, and supersession in one `HStack`, forcing the human-readable result to surrender width.
4. **P1 — Checks have no internal information architecture.** They follow prose without a heading, breakdown, issue group, or large-history affordance.
5. **P1 — Disclosure headers are overloaded and under-described.** Session and step buttons combine many visual fragments without explicit accessibility labels, values, hints, or section semantics.
6. **P1 — Recovery and representative coverage are thin.** Session-load failure has no retry, and the canonical fixture’s small one-check steps cannot guard the supplied dense failure mode.

## Recommended SwiftUI-native direction

- Preserve the honest `SessionDrillRow → StepCard → Check history` hierarchy; do not flatten it into another dashboard table.
- Split the step header into two tiers:
  - primary: disclosure, evidence pip, full title, grade;
  - secondary: lifecycle, recency, kind, and a result summary such as `23 passed · 1 failed · 1 superseded`.
- Order expanded content as: blocker/next action; outcome and evidence explanation; **Needs attention**; **Check history**; files and supporting context.
- Keep live failed/error checks visible. For large histories, show a representative preview and an explicit `Show all N checks`; never count superseded failures as live issues.
- Render each check with a flexible first line for result symbol plus full summary, then a muted metadata line such as `test · passed · exit 0 · agent-reported · 2h ago`. Reserve extra detail for resolution, artifact, files, scope, and redaction.
- Use `ViewThatFits(in: .horizontal)` to prefer the regular row and fall back to stacked metadata for narrow widths, large text, and localization.
- Add a pure, testable `StepCheckSummary`-style derivation for live pass/fail/error/skipped/superseded and source counts. Keep ordering and count semantics outside view code.
- Add disclosure `accessibilityLabel`, `accessibilityValue`, and `accessibilityHint`; mark subsection headings; hide decorative chevrons/pips; give each check one coherent spoken result.
- Add a Retry action for session-load failure without losing the expanded context.
- Extend fixtures and snapshots with 25 mixed checks, 100 checks, long text, missing fields, mixed sources, fail→pass supersession, load failure, narrow width, large text, light, and dark.
- Do not replace row-level trust with an “all independent” shortcut: provenance remains check-specific even when an aggregate reduces repetition.

## Primary-source notes

- **Claim:** Hierarchical text belongs in an outline/list pattern; succinct rows should disclose detail instead of becoming oversized. **Source:** Apple, *Lists and tables*, current page accessed 2026-08-28, https://developer.apple.com/design/human-interface-guidelines/lists-and-tables. **Confidence:** High. **Gap:** General HIG guidance, not specific to evidence ledgers.
- **Claim:** Keep essential information visible and hide detail until relevant; disclosure labels must describe what they reveal. **Source:** Apple, *Disclosure controls*, current page accessed 2026-08-28, https://developer.apple.com/design/human-interface-guidelines/disclosure-controls. **Confidence:** High. **Gap:** The warning about multiple disclosure buttons applies most directly to disclosure buttons; nested outline triangles remain valid when hierarchy is clear.
- **Claim:** SwiftUI can adapt horizontal content to a vertical alternative for large text and localization using `ViewThatFits`. **Source:** Apple, *Composing custom layouts with SwiftUI*, current page accessed 2026-08-28, https://developer.apple.com/documentation/swiftui/composing-custom-layouts-with-swiftui. **Confidence:** High. **Gap:** Exact breakpoints still require snapshot testing.
- **Claim:** Explicit accessibility labels, values, hints, headings, and grouping are available when automatic inference is insufficient. **Source:** Apple, *Accessibility fundamentals* and *Accessible descriptions*, current pages accessed 2026-08-28, https://developer.apple.com/documentation/swiftui/accessibility-fundamentals and https://developer.apple.com/documentation/swiftui/accessible-descriptions. **Confidence:** High. **Gap:** VoiceOver behavior still requires built-app verification.
- **Claim:** Focus order must preserve meaning, and descriptive headings help users orient and scan. **Source:** W3C WAI, *Understanding SC 2.4.3: Focus Order* and *Understanding SC 2.4.6: Headings and Labels*, current pages accessed 2026-08-28, https://www.w3.org/WAI/WCAG22/Understanding/focus-order.html and https://www.w3.org/WAI/WCAG22/Understanding/headings-and-labels.html. **Confidence:** Medium-high. **Gap:** WCAG is web-normative; these are transferable native-app heuristics.
