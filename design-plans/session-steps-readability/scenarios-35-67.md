# Session and steps readability: accessibility scenarios 35–67

> Read-only specialist simulation lane: 33 realistic accessibility scenarios, **not 33 independent agents**. No code was changed in this lane.

## Scenarios

### 35. VoiceOver: first arrival

- **Role/context:** Blind reviewer entering the receipt.
- **Goal:** Jump directly to the session history.
- **Current failure:** “Sessions & steps” is visual styling only (`WorkPane.swift:1345`), not an accessibility heading.
- **Requirement:** Expose a heading trait and a concise section summary.
- **Priority:** P1

### 36. VoiceOver: collapsed session

- **Role/context:** Blind reviewer scanning sessions before opening one.
- **Goal:** Know what the disclosure contains and whether it is open.
- **Current failure:** SwiftUI synthesizes a label from chip, title, project, and time, but there is no explicit expanded/collapsed value or hint (`WorkPane.swift:1466–1489`); exact speech is unverified.
- **Requirement:** Label with the session title; value with role, step count, and state; hint that it shows or hides session steps.
- **Priority:** P0

### 37. VoiceOver: session load

- **Role/context:** Blind reviewer opening remote session detail.
- **Goal:** Know whether loading succeeds and recover if it fails.
- **Current failure:** Loading and failure are visual text; retry requires collapsing and reopening (`WorkPane.swift:1507–1551`).
- **Requirement:** Named loading status, completion/failure announcement, and a visible keyboard-accessible Retry action.
- **Priority:** P0

### 38. VoiceOver: collapsed step

- **Role/context:** Blind reviewer scanning a session’s steps.
- **Goal:** Hear title, lifecycle status, evidence, check count, and disclosure state as one unit.
- **Current failure:** No explicit accessibility label, value, or hint exists (`StepComponents.swift:64–109`).
- **Requirement:** One disclosure element whose value includes status, evidence tier, check count, and collapsed state.
- **Priority:** P0

### 39. VoiceOver: expanded step

- **Role/context:** Blind reviewer exploring one step deeply.
- **Goal:** Navigate a predictable information hierarchy.
- **Current failure:** Status, timestamp, usage, evidence reason, summary, checks, blocker, next step, and files are visually indented but not programmatically sectioned.
- **Requirement:** A contained group with headings such as Summary, Checks, Blocker, Next step, and Files when present.
- **Priority:** P1

### 40. VoiceOver: individual check

- **Role/context:** Blind reviewer validating one machine or agent-reported check.
- **Goal:** Hear one coherent result.
- **Current failure:** Status symbol, type, summary, exit code, source, and superseded chip remain separate visual children, and the icon is not hidden (`StepComponents.swift:229–254`).
- **Requirement:** Combine each row into one element and explicitly announce result, full summary, exit, source, and supersession.
- **Priority:** P0

### 41. VoiceOver: failed then superseded

- **Role/context:** Blind reviewer comparing a retained failure with its later resolution.
- **Goal:** Distinguish active risk from history.
- **Current failure:** A visible X and “superseded” exist, but the accessibility representation does not explicitly say that the failure is historical.
- **Requirement:** Announce active versus historical state and never let a superseded failure sound current.
- **Priority:** P0

### 42. Keyboard-only review

- **Role/context:** Motor-impaired reviewer using no pointer.
- **Goal:** Tab through sessions and steps and activate with Space or Return.
- **Current failure:** Native `Button` and `SurfaceButtonStyle` are promising and include a visible focus overlay, but the real macOS keyboard path has not been exercised.
- **Requirement:** Preserve native buttons and verify Tab, Shift-Tab, Space, Return, and scroll-into-view.
- **Priority:** P0 verification

### 43. Focus after collapse

- **Role/context:** Keyboard user collapsing a long 25-check step.
- **Goal:** Continue from the same control.
- **Current failure:** Focus retention is untested while substantial content disappears beneath the trigger.
- **Requirement:** Keep focus on the disclosure with no jump to the window or another session.
- **Priority:** P0 verification

### 44. Switch Control

- **Role/context:** User scanning controls sequentially.
- **Goal:** Identify each session and step with minimal scan time.
- **Current failure:** Synthesized names may be long and repetitive, especially around root/subagent chips.
- **Requirement:** Concise unique control names containing session role or distinguishing ID; no focusable static metadata.
- **Priority:** P1

### 45. Voice Control

- **Role/context:** User activating controls by speaking visible labels.
- **Goal:** Open a named session or step reliably.
- **Current failure:** Duplicate or truncated titles can be ambiguous, and no alternate input labels are supplied.
- **Requirement:** Preserve the visible title in the accessible name and add short unique alternate labels only for duplicates.
- **Priority:** P1

### 46. Low vision at 2× macOS Zoom

- **Role/context:** User magnifying the window and reading one line at a time.
- **Goal:** Follow a check without horizontal scanning.
- **Current failure:** The all-inline check `HStack` creates a long line and squeezes the summary.
- **Requirement:** Put result and type first, the full summary in a flexible block, and metadata on a separate trailing or lower line.
- **Priority:** P0

### 47. Screen magnifier with focus tracking

- **Role/context:** Keyboard user following the focus rectangle under magnification.
- **Goal:** Maintain a stable viewport while expanding content.
- **Current failure:** Focus styling exists, but expansion causes a large vertical change and its scroll behavior is unverified.
- **Requirement:** Keep focus stationary, avoid automatic scroll jumps, and keep the focused row visible.
- **Priority:** P1 verification

### 48. Low contrast sensitivity

- **Role/context:** Reviewer reading 12-point secondary text for an extended period.
- **Goal:** Separate result, evidence, and provenance without strain.
- **Current failure:** Source token values calculate to approximately 6.14:1 in light mode and 7.04:1 in dark mode for muted text on cards, so there is no static AA text-contrast blocker; density and weak hierarchy remain.
- **Requirement:** Retain contrast while improving spacing, line length, and visual hierarchy.
- **Priority:** P1

### 49. Increase Contrast enabled

- **Role/context:** User relying on stronger component boundaries.
- **Goal:** Perceive nested cards and disclosures clearly.
- **Current failure:** Custom colors define light/dark values only and ignore `colorSchemeContrast`; card-line/card contrast is about 1.4:1 in both appearances.
- **Requirement:** Use a stronger section/card boundary or system-adaptive stroke under increased contrast and verify both appearances.
- **Priority:** P1

### 50. Light mode in a bright room

- **Role/context:** Reviewer working with glare.
- **Goal:** Scan nested session, step, and check levels.
- **Current failure:** Subtle borders and repeated muted metadata weaken grouping.
- **Requirement:** Make whitespace, headings, and row structure carry hierarchy without depending on faint borders.
- **Priority:** P1

### 51. Dark mode in low light

- **Role/context:** Reviewer working in a dim environment.
- **Goal:** Find meaningful results without reading a gray wall of text.
- **Current failure:** Text contrast passes, but nearly every secondary token has the same muted treatment.
- **Requirement:** Separate primary result, summary, and tertiary provenance by layout and weight while retaining contrast.
- **Priority:** P1

### 52. Protanopia or deuteranopia: pass versus fail

- **Role/context:** Reviewer who cannot depend on green/coral distinctions.
- **Goal:** Identify result state immediately.
- **Current failure:** Checkmark and X shapes help, but no visible result word accompanies every check.
- **Requirement:** Retain distinct glyphs and show Passed, Failed, Error, or Skipped text.
- **Priority:** P0

### 53. Color-vision deficiency: evidence tiers

- **Role/context:** Reviewer comparing claimed, self-checked, and independent evidence.
- **Goal:** Distinguish tiers without hue.
- **Current failure:** Pip shapes and tier words already provide redundancy, although the 8-point pip is small.
- **Requirement:** Preserve text labels and never collapse the tier to color or pip alone.
- **Priority:** P1

### 54. Achromatopsia or grayscale

- **Role/context:** User seeing luminance and form only.
- **Goal:** Understand hierarchy and status without color.
- **Current failure:** Most semantics survive through words and glyphs, but card nesting is weak.
- **Requirement:** Inspect grayscale snapshots and retain non-color grouping and result labels.
- **Priority:** P1 verification

### 55. Differentiate without Color enabled

- **Role/context:** User explicitly requesting redundant cues.
- **Goal:** Operate the section with color removed.
- **Current failure:** The environment value is not read, although the design already uses several shapes.
- **Requirement:** Explicit result words and disclosure state values should make the standard layout sufficient without a special alternate UI.
- **Priority:** P1

### 56. Reduce Motion

- **Role/context:** Vestibular-sensitive user repeatedly opening disclosures.
- **Goal:** Avoid spatial animation.
- **Current failure:** No current code failure: both session and step animations disable themselves when `accessibilityReduceMotion` is true.
- **Requirement:** Preserve and regression-test this behavior; do not substitute another spatial animation.
- **Priority:** P1 verification

### 57. VoiceOver plus asynchronous content change

- **Role/context:** User opening a session while speech is active.
- **Goal:** Learn when loading finishes without losing context.
- **Current failure:** No explicit announcement indicates success or failure.
- **Requirement:** Update the disclosure value and provide a polite status announcement without stealing focus.
- **Priority:** P1

### 58. Forced Dynamic Type stress

- **Role/context:** QA injecting `.accessibility3` to reveal clipping.
- **Goal:** Test resilience under unusually large type.
- **Current failure:** Fixed-size typography in this section does not meaningfully scale.
- **Requirement:** Retain forced large-type rendering as a stress test and test actual users with macOS Zoom. Apple documents that user-selected Dynamic Type does not affect macOS text size, so this is not a user-facing conformance claim.
- **Priority:** P2

### 59. Minimum supported 960-point window

- **Role/context:** User reviewing beside the 204-point receipt rail.
- **Goal:** Read every meaningful field at the supported minimum.
- **Current failure:** Title, kind, check count, tier, exit, provenance, and summary compete in fixed `HStack`s.
- **Requirement:** Provide a compact vertical fallback with no lost title, result, or summary.
- **Priority:** P0

### 60. Resized split-screen window

- **Role/context:** Reviewer comparing the app with a PR or terminal.
- **Goal:** Narrow the window without losing evidence.
- **Current failure:** Metadata is allowed to compress semantic content first.
- **Requirement:** Prioritize result and summary and move metadata below before truncating meaningful content.
- **Priority:** P0

### 61. Very long step title

- **Role/context:** Reviewer inspecting an automatically generated objective.
- **Goal:** Recover the complete title and understand how to reveal it.
- **Current failure:** The collapsed title is one line; the full title appears only after activation, but the accessibility hint does not explain that.
- **Requirement:** Allow two visual lines where possible, expose the full title to assistive technology, and clearly indicate disclosure behavior.
- **Priority:** P1

### 62. Very long check summary

- **Role/context:** Reviewer needing exact verification evidence.
- **Goal:** Read the complete expanded result.
- **Current failure:** `lineLimit(2)` truncates the summary even though the expanded-detail comment promises no truncation (`StepComponents.swift:238–241`).
- **Requirement:** Remove the limit in expanded detail and let summary wrap independently of metadata.
- **Priority:** P0

### 63. Long project, model, or path metadata

- **Role/context:** Reviewer inspecting generated identifiers and mixed-width text.
- **Goal:** Read or select long values without distorting the primary content.
- **Current failure:** Metadata shares non-wrapping `HStack`s and can crowd the title.
- **Requirement:** Flexible metadata rows, breakable/selectable long text, and semantic priority over decorative chips.
- **Priority:** P1

### 64. German-style text expansion

- **Role/context:** Reviewer using a localization with substantially longer labels.
- **Goal:** Preserve layout under 30–50% expansion.
- **Current failure:** Hard-coded English copy and inline pills assume short strings.
- **Requirement:** Avoid fixed semantic rails in this section and snapshot an expanded-string fixture; full-app localization is outside this focused PR.
- **Priority:** P1

### 65. Arabic RTL

- **Role/context:** Reviewer using an RTL interface while evidence contains English commands.
- **Goal:** Preserve reading order and relationships.
- **Current failure:** SwiftUI stacks are leading/trailing-friendly, but the surface has no RTL snapshot or VoiceOver-order verification.
- **Requirement:** Mirror disclosure layout, align lists consistently, preserve numeral and identifier order, and test VoiceOver order.
- **Priority:** P1

### 66. CJK plus Latin identifiers

- **Role/context:** Step title is Chinese or Japanese while model, exit code, and source remain Latin.
- **Goal:** Scan mixed-script content without awkward wrapping.
- **Current failure:** A single baseline `HStack` creates uneven wrapping and scanning.
- **Requirement:** Content-language paragraph alignment, an independent metadata line, and no assumption that spaces provide wrap points.
- **Priority:** P1

### 67. Cognitive fatigue with 25 checks

- **Role/context:** Reviewer trying to find the one active failure in a long receipt.
- **Goal:** Reach the decision-relevant evidence first.
- **Current failure:** Every row has equal weight, `(exit 0)` and `agent-reported` repeat, and the active failure is buried in an undifferentiated ledger.
- **Requirement:** Headline counts, a labeled Needs attention group first, stable order within active/history groups, and progressive “Show all N checks” disclosure.
- **Priority:** P0

## Ranked fixes

1. **P0:** Replace the one-line check ledger (`StepComponents.swift:229–254`) with a two-tier row: visible result word/glyph, flexible full summary, then exit/source/supersession metadata. Remove `lineLimit(2)`.
2. **P0:** Give session and step buttons explicit label, value, and hint. Values must contain expanded/collapsed state; labels must not repeat every visual chip.
3. **P0:** Add a checks summary and progressive reveal for dense steps. Show active failures/errors before ordinary passes; keep superseded history explicitly separate and preserve stable ordering.
4. **P0:** Add a real session-load failure state with Retry and accessible loading/success/failure status. Keep keyboard focus on the disclosure.
5. **P1:** Mark “Sessions and steps” and internal content groups as accessibility headings/containers.
6. **P1:** Add compact vertical fallbacks before semantic text truncates. Treat result and summary as higher priority than kind, time, model, exit, and provenance.
7. **P1:** Respond locally to increased contrast with stronger boundaries; retain existing light/dark text tokens, whose source-value contrast passes AA.
8. **P1:** Preserve existing native `Button`, focus-ring, color-redundancy, and Reduce Motion behavior.

## Verification implications

- Add pure presentation tests for disclosure labels/values, active versus superseded failure wording, result-count summaries, empty checks, mixed provenance, and duplicate titles.
- Add a focused Session/Steps snapshot harness covering regular, minimum-width, 25-check dense, long-string, RTL, and increased-contrast states in light and dark.
- Ensure the focused renderer rejects clipped canvases and renders deterministically twice.
- Manually verify Tab and Shift-Tab navigation, Space and Return activation, visible focus, scroll-into-view, and focus retention after collapse.
- Manually verify with VoiceOver: heading navigation; session role/title/count/state; step title/status/evidence/check count/state; one complete failed and superseded check; loading, failure, and Retry.
- Run Accessibility Inspector on collapsed, expanded-dense, loading, and failed-load states. Apple notes that a clean automated audit does not replace assistive-technology testing.
- The repository currently has no host-app XCUITest target (`apps/agentacct/Tests/README.md:556`), so automated `performAccessibilityAudit` is unavailable without materially expanding project scope.

## Primary-source ledger

- **Claim:** Accessible interfaces should support larger text, sufficient contrast, and non-color cues. **Source:** “Accessibility,” Apple Human Interface Guidelines, updated June 9, 2025. https://developer.apple.com/design/human-interface-guidelines/accessibility/ **Confidence:** High.
- **Claim:** VoiceOver needs descriptive labels, headings, explicit grouping/order, and notice of layout changes. **Source:** “VoiceOver,” Apple Human Interface Guidelines, current, accessed 2026-08-28. https://developer.apple.com/design/human-interface-guidelines/voiceover **Confidence:** High.
- **Claim:** SwiftUI supplies labels, values, hints, actions, headings, and accessibility child behavior. **Source:** “Accessibility modifiers,” Apple Developer Documentation, current. https://developer.apple.com/documentation/SwiftUI/View-Accessibility **Confidence:** High.
- **Claim:** Custom colors should work in light, dark, and increased-contrast contexts. **Source:** “Color,” Apple Human Interface Guidelines, current. https://developer.apple.com/design/human-interface-guidelines/color **Confidence:** High.
- **Claim:** `colorSchemeContrast` exposes the user’s standard/increased contrast setting. **Source:** Apple Developer Documentation, current. https://developer.apple.com/documentation/swiftui/environmentvalues/colorschemecontrast **Confidence:** High.
- **Claim:** SwiftUI exposes Dynamic Type size through the environment, so scalable font roles and injected larger sizes can be verified; the exact user-facing macOS text and Zoom behavior still needs manual testing. **Source:** “dynamicTypeSize,” Apple Developer Documentation, current. https://developer.apple.com/documentation/swiftui/environmentvalues/dynamictypesize **Confidence:** Medium.
- **Claim:** RTL layouts should reverse consistently and align lists and paragraphs according to reading and language direction. **Source:** “Right to left,” Apple Human Interface Guidelines, current. https://developer.apple.com/design/human-interface-guidelines/right-to-left **Confidence:** High.
- **Claim:** Accessibility Inspector detects clipping, missing descriptions, contrast, and text-size issues, but assistive-app testing remains necessary. **Source:** “Performing accessibility audits for your app,” Apple Developer Documentation, current. https://developer.apple.com/documentation/accessibility/performing-accessibility-audits-for-your-app **Confidence:** High.
- **Claim:** Color must not be the only carrier of meaning. **Source:** W3C, “Understanding SC 1.4.1 Use of Color,” updated September 16, 2025. https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html **Confidence:** High.
- **Claim:** Text should resize without lost content and meaningful text should reflow rather than require two-dimensional reading. **Sources:** W3C WCAG 2.2, “Resize Text” and “Reflow,” current. https://www.w3.org/WAI/WCAG22/Understanding/resize-text.html and https://www.w3.org/WAI/WCAG22/Understanding/reflow.html **Confidence:** High.
- **Claim:** All functionality needs keyboard access, logical focus order, and visible focus. **Sources:** W3C WCAG 2.2, current. https://www.w3.org/WAI/WCAG22/Understanding/keyboard.html, https://www.w3.org/WAI/WCAG22/Understanding/focus-order.html, and https://www.w3.org/WAI/WCAG22/Understanding/focus-visible.html **Confidence:** High.
- **Claim:** Controls need programmatic names, roles, values, and expanded/collapsed state. **Source:** W3C WCAG 2.2, “Name, Role, Value,” current. https://www.w3.org/WAI/WCAG22/Understanding/name-role-value.html **Confidence:** High.

## Limitations

- This was a read-only static/code review. VoiceOver, Switch Control, Accessibility Inspector, increased-contrast rendering, and live keyboard behavior were not executed in this lane.
- WCAG is a web standard rather than a native macOS certification target; it is used here as a conservative acceptance baseline where Apple’s guidance is qualitative.
- Apple and W3C do not prescribe the proposed failure-first grouping or reveal threshold. Those are design inferences from the observed 25-check cognitive-load failure.
