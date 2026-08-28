# Work list and receipt UX harmony review

Status: the smallest coherent redesign slice is implemented on `codex/work-ux-harmony`; the scenario ledger also records follow-up opportunities that remain deliberately out of scope.

Review baseline: the supplied screenshots and the pre-change app. The review began from `ed4c3fb`; the final implementation is rebased onto `732b3aa` from `origin/main`, preserving the subsequently merged interaction and reduced-motion work.

## Method and confidence

This review combines:

- the two supplied screenshots;
- read-only inspection of the running `agentacct` app;
- source and canonical reference-image inspection;
- one primary review plus three independent specialist streams;
- 100 non-overlapping scenarios: novice/decision flows (1-34), expert/scale flows (35-67), and accessibility/degraded states (68-100).

The environment allowed three subagents concurrently, not 100 simultaneous independent agents. The 100-scenario ledger below is real scenario coverage, not a claim of 100-agent capacity.

## Executive decision

The mismatch is structural, not a palette problem. The list is a wide comparison ledger; opening a row destroys it and replaces it with a fixed 204-point receipt rail plus a long forensic document. Search, lifecycle filter, focus, and scroll context disappear, while only sort survives. The rail then uses different density, fields, and status styling from the list.

The best direction is an **adaptive persistent Work workbench**:

1. Keep one receipt collection, one browse state, and one visual/semantic row system.
2. With no selection, let the collection use the full width as a comparison table.
3. With a selection on a wide window, condense that same filtered collection into a resizable master and open detail beside it.
4. On narrow windows or accessibility text sizes, use push navigation and remove the fixed rail.
5. Lead detail with why the receipt has its current status and what the user can do; move raw instrumentation and provenance behind progressive disclosure.

Central rule: **selection changes focus, not the product mode**.

## What is actually broken

### 1. The working set is destroyed

`WorkPane` swaps `WorkTablePage` for `recordLayout` when a task is selected. Query and lifecycle group are local state owned by the destroyed table, while sort alone is shared. A live test confirmed that filtering for `pytest`, opening the only result, and pressing Escape returns to an unfiltered All list.

Relevant pre-change code: `apps/agentacct/Sources/agentacct/WorkPane.swift` (`WorkPane`, `WorkTablePage`, and the former receipt rail).

### 2. Summary and detail can look contradictory

The screenshots show the same receipt with different cost values in list, rail, and detail, and with list checks “not reported” while detail reports 9/11. Even when the payloads are individually honest, the interface provides no shared revision, source, or reconciliation explanation.

The words also invite conceptual confusion:

- `0/1 checked` is claim-step evidence coverage.
- `9/11 passed` is recorded check-run outcome.

Those are different axes, but both currently read as “checks.” They should appear together as, for example, **Claims supported: 0/1** and **Check runs: 9 passed, 2 failed**.

### 3. Decision-critical information is below telemetry

A Finding or Blocked receipt leads with KPI totals and Receipt dimensions. The failing check, blocker, or next useful action may be below the fold or inside a disclosure. The first question should be: “Why does this need me, and what can I do?”

Relevant pre-change code: `WorkRecordPage`, `RecordDimensionsCard`, and `RecordChecksCard`.

### 4. The list and rail are different products

The table uses a decision badge, evidence coverage, client, check runs, cost, and recency. The rail uses uppercase status text, title, cost, client, and recency; it omits evidence and check state, has no query or lifecycle filter, and does not expose selected state to accessibility APIs.

Relevant pre-change code: `WorkTableRow`, `WorkRail`, and `WorkRailRow`.

### 5. Accessibility and degraded states are incomplete

The full Work page uses mostly fixed typography; only the focused Actions digest has Dynamic Type stress coverage. Custom table-row accessibility text omits client, checks, cost, recency, and handoff. Focus transfer/restoration is not modeled. Initial loading can look like a true empty result. Cached rows can remain silently visible after refresh failure, and receipt errors are raw text without Retry or a stable Back control.

### 6. Scale is bounded but the UI sometimes sounds global

The list loads at most 200 receipts and filters/sorts them locally. Search cannot reach older receipts, yet the empty-result language says nothing is hidden. Rows are eagerly composed in ordinary stacks, and periodic refresh can reorder them without preserving focus or scroll.

## What already works

- The list and detail are individually clean, disciplined, and visually consistent with the agentacct palette and type system.
- Core light and dark text/token pairs meet WCAG AA in the reviewed palette; the primary problem is not contrast.
- Decision status and evidence strength remain separate instead of falsely upgrading agent claims.
- Missing data is usually named rather than silently converted to zero.
- Canonical snapshots already cover light/dark, minimum/reference widths, empty, loading, and error states.

These should be preserved.

## Options

| Option | Model | Benefits | Costs and risks | Recommendation |
|---|---|---|---|---|
| **A. Adaptive persistent ledger + detail** | The full table condenses into a resizable master when a receipt opens; the same filtered collection, row semantics, and browse state remain mounted. Narrow/large-text layouts push detail over the list. | Best continuity, comparison, keyboard behavior, and use of wide screens; one mental model | Requires column-priority rules, durable browse state, focus/scroll restoration, and responsive tests | **Choose this** |
| **B. Full-width list to full-width receipt** | Remove the rail. Use conventional push navigation with Back and filtered previous/next controls. | Simplest implementation and strongest narrow-window/Dynamic Type behavior | Slower cross-receipt comparison; wide windows underused | Best conservative fallback |
| **C. Table + triage inspector + full receipt** | Keep the table visible; selection opens a right-side summary inspector, with a separate Open full receipt action. | Fastest high-volume triage and comparison | Creates preview versus full-detail ownership and three navigation states | Valuable later, not the first redesign |
| **D. Permanent compact inbox + detail** | Always show a 320-380 point receipt inbox; comparison table becomes a secondary mode. | Very stable spatial model | Weakens the excellent wide comparison ledger and adds a mode to recover it | Do not choose first |

## Recommended interaction and information architecture

### Persistent Work shell

- Header: Work receipts, loaded/total count, freshness, refresh state.
- Primary view filter: Needs attention, Active, Finished, All.
- Secondary filters: decision status, evidence level, lifecycle disposition, client, project, cost, time, sessions.
- Search and sort remain visible in the master and persist across navigation.
- Browse state owns query, filters, sort, density, scroll position, focused row, and selected task.

Raw decision words remain visible on each row. Goal-oriented filters do not rewrite receipt truth; they only group it.

### One receipt-row system

Expanded table and compact master should derive from one presentation model:

- task title and decision badge;
- plain-language attention reason when applicable;
- claim coverage and strongest evidence tier;
- failed/check-run outcome;
- cost state and basis;
- client and freshness;
- explicit handoff/lifecycle disposition.

At narrower widths, lower-priority columns reflow into a second line instead of disappearing or silently truncating.

### Decision-first detail

Order the detail page as:

1. Title, decision status, disposition, scope, and freshness.
2. `Why this status` callout with the latest blocker/failing check and available review/resolve action.
3. One proof summary pairing **Claims supported** with **Check runs** without merging their meanings.
4. Cost, elapsed time, sessions, and changed scope.
5. Failed/open checks first, then all checks.
6. Sessions and steps.
7. Recorded tool activity.
8. Technical details: schema, raw IDs, token accounting, provenance source keys, gaps, and capture boundaries.

Suggested detail navigation: Overview, Evidence, Activity, Technical.

### Responsive behavior

```text
Wide, selected
┌──────────────── persistent Work controls ──────────────────────────────┐
│ Receipts master (resizable) │ Receipt: decision-first detail           │
│ same filters and result set │ Why this status                           │
│ same row semantics          │ Claims supported | Check runs | Cost      │
│ selected row stays visible  │ Overview · Evidence · Activity · Technical│
└─────────────────────────────┴───────────────────────────────────────────┘

Wide, no selection
┌──────────────── persistent Work controls ──────────────────────────────┐
│ Full comparison ledger                                                  │
└─────────────────────────────────────────────────────────────────────────┘

Narrow or accessibility text
┌──────────────── Work list ───────────────┐  →  ┌──── Receipt detail ────┐
│ persistent browse state              │      │ Back restores row/focus │
└───────────────────────────────────────┘      └──────────────────────────┘
```

The distinctive design signature should be **ledger continuity**: the same decision, proof, cost, and freshness facts keep the same wording and visual grammar as a row condenses into a master item and expands into the receipt header.

## 100-scenario ledger

Severity: H = can cause a wrong decision, lost work, inaccessible core task, or material misrepresentation; M = substantial friction; L = bounded polish.

### First-time, occasional, and decision-making flows (1-34)

| # | Scenario | Current risk | Required UX response | Sev |
|---|---|---|---|---|
| 1 | Brand-new user, no receipts | True empty state looks like a filter miss | Explain receipts and provide Setup recording, Sources, and Refresh actions | H |
| 2 | First populated visit | “Receipt,” evidence, and status lack orientation | Add one plain-language sentence explaining claim, work, and proof | M |
| 3 | Return after several days | Attention rows do not explain why they need attention | Use Needs attention and show a row-level reason/next action | H |
| 4 | Seven status tabs, several zero | Internal taxonomy dominates the first decision | Lead with goal-oriented groups; keep exact statuses in filters/rows | M |
| 5 | Unfamiliar status | Tiny detached glossary requires translation | Make the badge reveal contextual meaning and consequence | M |
| 6 | Hovering a row | Hover and selected wash are too similar; open action is implicit | Distinct hover/focus/selection plus a disclosure affordance | M |
| 7 | Long or multilingual title | One/two-line truncation makes tasks indistinguishable | Reflow, expose full title on focus/hover, preserve accessible value | M |
| 8 | Search by remembered phrase | Query is destroyed by opening detail | Persist query in Work browse state | H |
| 9 | Search + status + sort | Only sort survives the round trip | Persist all filters, scroll, focus, and selection | H |
| 10 | Open one filtered result | Rail suddenly shows every receipt | Master represents the active result set and position | H |
| 11 | Confirm selected task | Title and converged objectives can look unrelated | Show scope summary and explain converged sessions/objectives | H |
| 12 | Return from detail | Back button plus permanent rail imply competing models | Use adaptive master-detail or pure push, not both at once | H |
| 13 | Triage adjacent receipts | Tiny rail drops evidence/check context | Use a wider adaptive master with the same row grammar | H |
| 14 | Compare expensive/risky tasks | Repeated backtracking resets context | Preserve comparison context or add a later triage inspector | H |
| 15 | Change sort | Rail uses an unlabeled icon and hides current order | Keep labeled sort control in persistent master header | M |
| 16 | Data refreshes during review | Selection can move and values can diverge | Pin selection; reconcile snapshots or label revision/time | H |
| 17 | Enter from a primary session | Session silently resolves to a converged task | State “Opened from session” and link to its session section | M |
| 18 | Enter from unresolved subagent | Error provides no direct recovery path | Find in receipts with prefilled session/client context | H |
| 19 | Receipt loading | Generic spinner loses selected identity | Keep stable shell, selected title, Back, and skeleton | M |
| 20 | Receipt/list request fails | Raw text has no Retry or diagnostics path | Structured error with Retry, Back/results, and diagnostics | H |
| 21 | Filter has zero results | Active constraints are not visible or clearable | Show filter chips and Clear filters | M |
| 22 | Finding vs Failed vs Blocked | Operational difference is buried | Row reasons: failing check, agent failure, waiting on blocker | H |
| 23 | Reported vs Verified | Users may equate a claim with proof | Pair a plain claim sentence with evidence coverage | H |
| 24 | `0/1 checked` | “Checked” is ambiguous | Rename to Claims supported or equivalent explicit copy | H |
| 25 | Evidence `0/1`, Checks `9/11` | Two legitimate axes look contradictory | Pair Claim coverage and Check runs in one proof summary | H |
| 26 | List says not reported, detail has checks | User cannot identify authority/freshness | Shared revision or explicit summary/detail source explanation | H |
| 27 | Evidence pips | Legend is off-screen and opaque on first use | Add short text grade and contextual help | M |
| 28 | Not gradeable | Reads like a bad grade | Say No verifiable claim recorded and explain neutrality | H |
| 29 | Raw evidence sources | `client_log`, `mcp`, and `transcript_scan` require system knowledge | Human labels first; raw keys in Technical | M |
| 30 | Estimated cost changes | Prefix, basis, completeness, and time are too subtle | One snapshot amount plus visible estimate/partial/source/time | H |
| 31 | Unpriced cost | Could mean free rather than unknown | Say Cost unknown; explain usage/pricing state | M |
| 32 | Open a Finding | Failing item/action is below telemetry | Put Why this needs attention directly below the header | H |
| 33 | Large Actions total | Instrumentation count can masquerade as intent/progress | Rename Recorded tool activity and state the capture boundary | M |
| 34 | Need exact steps after summary | Progressive hierarchy is weak | Decision → open issues → proof → sessions → technical | H |

### Expert and high-volume operation (35-67)

| # | Scenario | Current risk | Required UX response | Sev |
|---|---|---|---|---|
| 35 | Scan 29 receipts | Hidden depth and similar hover/selection slow triage | Sticky header, visible scrollbar, durable selection, density choice | M |
| 36 | Scan 200 loaded receipts | Eager stacks and full replacements can redraw/jump | Native/lazy collection, stable IDs, diffed updates | H |
| 37 | Store exceeds 200 | Local search cannot reach older work | Server search/pagination and explicit loaded scope | H |
| 38 | Process Attention sequentially | Filter is destroyed on first open | One persistent master collection | H |
| 39 | Trust lifecycle counts | Group counts look global but cover loaded slice | Server aggregates or label every count as loaded-slice | M |
| 40 | Keyboard search | No Cmd-F, clear action, or result announcement | Native searchable behavior and keyboard contract | M |
| 41 | Multi-facet investigation | Search covers only title, ID, and client | Filters for status, evidence, client, project, cost, time, sessions | H |
| 42 | No search match | Copy overclaims scope and lacks recovery | Name scope/filters and add Clear search/all | M |
| 43 | Live Latest queue | Refresh reorders under pointer/focus | Deterministic sort, diff markers, optional deferred reorder | M |
| 44 | Attention-first sort | Decision group misses evidence-derived attention | Explicit needs-attention projection with reason | H |
| 45 | Cost sort | Only latest 200; partial/unpriced hidden | All-store sort, stable tie-break, cost-state groups | H |
| 46 | Sort by evidence/client/sessions | Static headers block expert comparison | Sortable priority headers and optional secondary sort | M |
| 47 | Arrow through rows | Buttons require repeated Tab and open on selection | Selectable table/list semantics, arrows, Home/End, Enter | H |
| 48 | Keyboard lifecycle filtering | Many tab stops and weak announcements | Composite segmented semantics and selected/count announcement | H |
| 49 | Back to exact working set | Query/group/focus reset | Durable WorkBrowseState | H |
| 50 | Return to item 80 | Scroll position returns to top | Persist scroll by filter/sort; restore selected row | H |
| 51 | Selected row is far down | Rail starts at top and can hide selection | Shared scroll state or scroll selected task into view | H |
| 52 | Compare nearby details | Rail ignores filters and drops key fields | Same data source, rows, filters, and resizable master | H |
| 53 | Queue refresh | Rows jump with no update indication | Diff by task ID, preserve state, show pending updates | H |
| 54 | Refresh open receipt | Chrome says fresh while detail remains stale | Refresh list and selected receipt together or show separate times | H |
| 55 | Refresh fails with cached rows | Stale data stays silently visible | Stale banner with last success, reason, and Retry | H |
| 56 | Selected task disappears | Orphan detail remains without explanation | Tombstone state with as-of time and recovery actions | H |
| 57 | Rapidly switch receipts | Repeated blank spinners slow comparison | Bounded detail cache/prefetch inside stable shell | M |
| 58 | List/rail/detail values differ | Trust in cost/check facts erodes | Shared revision or visible source/time/delta reconciliation | H |
| 59 | Partial/estimated/unpriced cost | Table and rail hide basis/completeness | Compact qualifier plus full accessible detail | M |
| 60 | Cost outlier | Cause requires long detail scan | Optional complexity columns and cost inspector | M |
| 61 | Coverage and run totals differ | Summary omission looks like corruption | Explicit labels and mismatch/absence handling | H |
| 62 | Status conflicts with evidence | Attention reason disappears at compact density | Keep decision, proof, and attention reason adjacent | H |
| 63 | Multi-session task | Complexity is invisible until deep detail | Sessions/subagents field/filter and detail anchor | M |
| 64 | Trace failing subagent | Flat hierarchy and unresolved links obscure attribution | Root/continuation/subagent tree with per-session checks | H |
| 65 | Session detail fails | No visible retry or cause | Inline Retry; preserve other expansions | M |
| 66 | Resize 960 to ultrawide | Fixed columns/rail and hard breakpoint create mode shock | Adaptive split, column priorities, overflow, density tests | H |
| 67 | Debug failed check | Evidence is scattered across five sections | Sticky local navigation, failed-first view, deep links/copy | H |

### Accessibility and degraded/edge conditions (68-100)

| # | Scenario | Current risk | Required UX response | Sev |
|---|---|---|---|---|
| 68 | VoiceOver table browsing | Row label omits client, checks, cost, recency, handoff | One ordered decision-complete accessible row summary | H |
| 69 | VoiceOver filtering | Active result count is not announced | Announce selected group and result changes | M |
| 70 | VoiceOver receipt master | Rail lacks composite label and selected trait | Selectable sidebar semantics and complete row value | H |
| 71 | VoiceOver detail reading | Cards lack a navigable heading/region hierarchy | Page heading, section headings, named KPI groups | M |
| 72 | VoiceOver disclosure | Expanded/loading/error state is inconsistent | One disclosure primitive with state/result announcements | H |
| 73 | Keyboard controls | Excessive tab stops; no search shortcut | Roving group focus, Cmd-F, Escape clear, stable order | M |
| 74 | Keyboard receipt navigation | Every row is an independent button | Arrow navigation, Home/End, Enter, persistent selection | M |
| 75 | Resolve finding popover | Initial focus, cancel, restore, announcements unclear | Explicit focus lifecycle, Escape, Cmd-Return, feedback | M |
| 76 | Open a receipt by keyboard/VO | Focus can land nowhere meaningful after mode swap | Focus receipt heading; retain origin for restoration | H |
| 77 | Return/refresh/error | Loading/error lacks Back and restoration | Stable shell and focus restoration to surviving row | H |
| 78 | Visible keyboard focus | Plain custom controls may not show verified ring | Shared focus treatment and 3:1 rendered-state tests | H |
| 79 | Monochrome/color-vision use | Rail selection is mostly color/bold/bar | Explicit visual and semantic Selected state; spell out handoff | M |
| 80 | Increase Contrast | Several semantic component states are untested | Rendered component-state contrast matrix | M |
| 81 | Reduce Transparency | Global fallback exists but Work is unverified | Add Work list/detail coverage under the setting | L |
| 82 | Reduce Motion | Several disclosures animate unconditionally | Centralized reduced-motion-aware disclosure behavior | M |
| 83 | Large text in list/master | Fixed type, columns, heights, rail, and line limits clip | Semantic scaling, growing rows, stacked compact layout | H |
| 84 | Large text in detail | Only Actions has focused scaling support | Full-page Dynamic Type reflow and stack rules | H |
| 85 | German text expansion | Hard-coded English/manual formatting and widths | Localization catalogs, plurals, locale formatting, stress fixtures | M |
| 86 | RTL/CJK/emoji/mixed bidi | Full page is only tested LTR | Full-page fixtures, bidi isolation, mirrored navigation | M |
| 87 | 960-point table | Six fixed columns crush the task title | Priority-based column collapse/reflow | H |
| 88 | 960-point detail | Fixed rail consumes scarce width | Hide rail; push navigation or receipt picker | M |
| 89 | Ultrawide | Shell changes still feel unrelated despite good line length | Stable Work shell; use space for comparison | L |
| 90 | Initial fetch | Loading looks like true empty/filter miss | Explicit initialLoading state and result announcement | H |
| 91 | True empty store | “No receipts match” gives wrong cause | Dedicated onboarding empty state with actions | M |
| 92 | Filtered empty | No direct clear/show-all action | Name constraints and provide one recovery action | L |
| 93 | Offline/daemon error with cache | Stale rows appear normal | Showing data from… banner, Retry, daemon/Sources actions | H |
| 94 | Slow receipt request | Spinner drops identity/back/timeout | Stable record shell, task identity, Back, bounded Retry | M |
| 95 | Missing/failed receipt | Blank detail with raw error | Structured error with Retry, All receipts, diagnostics | H |
| 96 | Session load failure | Generic error and implicit retry | Local retry, concise cause, announcement | M |
| 97 | Older/partial payload | Nil evidence tallies can become zero | Explicit unknown/none/partial/conflicting presentation models | H |
| 98 | Cost uncertainty via VO | Cost/basis/share are omitted or hover-only | Complete accessible cost value | M |
| 99 | Blocked/finding/handoff/ended-open | Outcome and lifecycle disposition are conflated in places | Present and announce both axes distinctly | H |
| 100 | Long/sensitive names, paths, artifacts | Truncation and screen-sharing can leak context | Wrap/reveal/copy plus share-safe masking mode | H |

## Acceptance criteria for the redesign

### Coherence and state

- Opening a receipt preserves query, filters, sort, scroll position, focus origin, and selected row.
- The active result set is identical in expanded-table and compact-master forms.
- List, master, and detail use the same words and presentation model for decision, evidence, checks, cost, lifecycle disposition, and freshness.
- A shared revision/as-of value reconciles summary and detail; if atomicity is unavailable, the UI names each source/time and the mismatch.

### Decision quality

- A user can answer “Does this need me, why, and what can I do?” without scrolling.
- Claims supported and Check runs are both visible, explicitly named, and never collapsed into one score.
- Finding, failed, blocked, handed off, ended open, reported, and verified remain semantically distinct.
- Unknown, none, partial, stale, failed, and not applicable are distinct states; missing values never become zero.

### Keyboard and accessibility

- VoiceOver and keyboard can complete browse → filter → open → expand → resolve → back without a mouse.
- Rows announce every decision-relevant field plus selected/expanded/loading states.
- Arrow keys navigate status groups and receipt collections; Enter/Space activate; Escape clears or returns according to context.
- Focus moves to the receipt heading on open and returns to the originating or nearest surviving row.
- Every custom control has a visible focus indicator with at least 3:1 non-text contrast.
- No status, proof tier, selection, error, or loading state relies on color alone.

### Responsive and global behavior

- Full Work list and receipt reflow without clipping at minimum/reference widths and accessibility text sizes.
- Full-page fixtures cover light/dark, Increase Contrast, Reduce Transparency, Reduce Motion, German expansion, Arabic RTL, CJK, emoji, long IDs/titles/paths/URLs, and 29/200/>200-receipt states.
- Initial loading, true empty, filtered empty, stale/offline, list error, receipt loading/error, disappeared selection, and session error are distinct and recoverable.
- Search and sort state their loaded/all-store scope. More-than-200 behavior uses server search/pagination or explicitly bounded results.

### Privacy

- Command text remains uncaptured.
- A share-safe mode can mask task titles, user-authored summaries, local paths, identifiers, and artifact references without destroying layout.

## Implemented first slice

The approved implementation follows the lowest-risk sequence:

1. A tested `WorkBrowseState` preserves query, lifecycle group, sort, and one-shot return focus across receipt round-trips.
2. One `WorkReceiptRowPresentation` supplies table and master semantics; selected rows prefer the full receipt so summary/detail values do not disagree.
3. The collection stays full-width with no selection, becomes a native resizable 320-480 point master beside detail at wide widths, and pushes detail at the 960-point minimum or accessibility text sizes.
4. Detail now starts with the status explanation and a proof summary that explicitly separates Claims supported from Check runs.
5. Initial loading, receipt error, true empty, filtered empty, stale list, and saved-detail refresh failure are visibly distinct; loading/error retain Back, and errors offer Retry.
6. Table/master rows expose decision-complete accessibility labels, selected traits, visible focus, keyboard and VoiceOver focus transfer, roving arrow movement, and one-shot restoration to the originating row or search fallback on Back. Periodic/manual refresh reads the current selection after the collection refresh, while receipt-list request generations prevent older list responses from overwriting newer data.
7. Work typography, receipt step details, badges, chips, and top chrome scale without fixed-height clipping. Thirty canonical full-window Work references cover light/dark, minimum/reference widths, first and maximum accessibility sizes, table/receipt, initial loading, empty, list error, receipt loading/error, saved stale detail, and a blocked attention receipt on the pinned renderer; twelve focused action references remain alongside them.
8. Claim coverage and check-run tallies use shared presentation contracts. Missing totals remain unreported, known partial passed/failed values remain visible, and only an explicit `gradeable: false` becomes Not gradeable. Explicitly truncated or total-less receipt collections name their loaded scope.
9. The Dashboard keeps its compact layout but now says `4/4 supported` instead of the ambiguous `4/4 checked`; its four canonical references were reviewed separately. Receipt and loading/error references also render the stable All receipts control, while the live path retains keyboard and VoiceOver focus transfer.

Deliberate follow-ups from the ledger include server-backed search/pagination beyond 200 receipts, broader localization coverage, exact scroll-position restoration, a bounded detail cache, failed-first section navigation, and share-safe masking. They require separate product/data contracts and are not smuggled into this UI slice.

Non-goals for the first slice:

- no new daemon status vocabulary;
- no fabricated per-action rows from aggregate tool-call categories;
- no speculative scoring or “probability of correctness”;
- no unrelated Dashboard, Usage, or Sources redesign (the Dashboard receives only the shared claim-coverage wording correction);
- no broad design-system rewrite.

## Evidence reviewed

- `apps/agentacct/Sources/agentacct/WorkPane.swift`
- `apps/agentacct/Sources/agentacct/ReceiptsPane.swift`
- `apps/agentacct/Sources/agentacct/DashboardStore.swift`
- `apps/agentacct/Sources/agentacct/Theme.swift`
- `apps/agentacct/Sources/agentacct/WorkSnapshotHarness.swift`
- `apps/agentacct/Tests/agentacctTests/DashboardInteractionTests.swift`
- `apps/agentacct/Tests/agentacctTests/WorkSnapshotHarnessTests.swift`
- `apps/agentacct/Tests/agentacctTests/ThemeContrastTests.swift`
- canonical `work-table-*`, `work-receipt-*`, `work-actions-*`, and four claim-wording `dashboard-*` reference images
- exact worktree bundle and isolated four-receipt daemon launch; Computer Use could not address the disposable app's Core Graphics window, so no live click result is claimed
