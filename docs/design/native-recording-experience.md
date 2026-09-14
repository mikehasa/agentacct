# Native recording and work review

Recording setup now opens in the main macOS window. A person chooses a coding
client, reviews the proposed user-level changes, installs, and receives explicit
activation guidance until a fresh event confirms capture. Existing work remains
reachable during recovery. Recording notices identify actionable causes without
treating a running daemon as proof of complete capture or usage attribution.

Work now places activity above and below one horizontal time axis. The visible
window determines which recorded events appear. Dragging the canvas or the
compact overview moves the window through time — axis ticks, labels and cards
are anchored to absolute time and move together; wheel input resizes the
visible span around the pointer and pinch changes the time scale.
Selecting an event opens its details below the timeline, without moving the
canvas. Dense groups offer a chooser so overlapping timestamps remain reachable. Comparison has been
removed from the current interface, including its drag targets and saved slots.
New snapshots accumulate while someone investigates. Reviewing arrivals preserves
the original records, filters, disclosure choices and native scroll positions.

![Central native time canvas](images/native-central-timeline.png)

The prior row-based screenshots and their evaluation remain historical artifacts;
they do not describe this implementation.

[Setup at 185% text in a compact window](images/native-setup-large.png) keeps
the install scope and manual-merge qualification beside the action.

## Current Work hierarchy

Work answers: which task needs attention, what is happening, and what evidence
supports a selected record? Its hierarchy is **task → activity → selected record
→ supporting details**. The default task view has no comparison placeholder,
empty detail sidebar, view-mode switch, button row for panning/zooming or
zero-failure announcement. Search remains close to activity. Current failures and data-quality limitations
remain visible when they occur.

The overview is subordinate to the activity surface. Selecting a record opens
its details directly below the timeline: actual title, result, source and
content. Files, Artifacts and Record details disclose supporting data on
request. Group chooser and record detail share the same region, with a way
back to the group. The region takes its natural height in the page flow, so
long content scrolls with the page instead of a nested panel. Close dismisses
the region; Escape also dismisses the group chooser. Reading size flows
through the ordinary view tree, so text and controls scale together. Supporting
records can be inspected even when their timestamps are outside the current
window. Task details reads as a visible receipt document below the timeline —
Usage, Sessions and Recording shown in full under caps section rules, with only
a genuinely long list collapsing behind a single counted control. Full
identifiers remain available (the task id copies from its row), and real
parent-section or later-result links remain contextual to the selected record.

This revision also fixes an interaction inconsistency: opening a task from the
complete-store Attention queue must not replace that queue with only the recent
receipt page. The same authoritative projection now drives both collection and
navigator, including unavailable/empty/partial states and return focus.

The local review package `design-plans/native-macos-overhaul/work-simplification/`
contains the page-purpose review, per-state hierarchy, exact string-bearing source
inventory, independent collection/receipt audits and direct validation record.
The earlier 100-agent scores do not rate this revision. No human-usability or
mathematical-optimum claim is made.

## Pointer and keyboard behavior

| Input | Behavior |
| --- | --- |
| Blank-canvas drag, or dragging the overview window | Move through time; ticks, labels and cards translate together |
| Wheel over the canvas or overview | Resize the visible time span around the pointer (scroll up to narrow it) |
| Pinch | Zoom around the pointer while keeping reading size unchanged |
| Overview edges | Resize one boundary of the window |
| Event click | Hold live movement and open the event's details |
| Canvas Left/Right, Home/End, +/− | Pan, reach history bounds, or change time scale |

System-reserved modified wheel gestures remain available to macOS. The visible
window never shrinks below a five-second span, keeping the axis and the
overview control meaningful, and may pan a small capped margin beyond the
recorded range so a card centered on the first or last record stays fully
reachable. Geometry and native input have separate tests; real interaction
checks are recorded separately from simulated events. No measured FPS or full
VoiceOver certification is claimed.

## Review the change in this order

| Area | Main files | Invariant to inspect |
|---|---|---|
| Read-only setup proposal | `src/agentacct/setup_preview.py`, `cli.py`, `tests/test_setup_preview.py` | Generate managed snippets without running setup or returning unrelated existing values. Unsupported configuration stays unresolved. |
| Capture lookup | `v1_sessions.py`, `api.py`, `SetupCaptureLookup.swift`, `SetupCaptureObserver.swift` | Filter client before pagination; confirm only the selected client's event after the effective boundary. |
| Setup and recovery | `NativeSetupFlow.swift`, `NativeClientActivationView.swift`, `SetupModel.swift`, `RecordingSetupRoute.swift` | Review scope before installation; reconnect preserves existing client configuration and guarded recorder ownership. |
| Health | `RecordingHealth.swift`, `RecordingHealthViews.swift`, `RecordingConnectionHistory.swift`, `SourcesPane.swift` | Reachability, capture, importer health and historical coverage remain distinct. Current causes precede routine diagnostics. |
| Saved work | `SavedWorkSnapshot.swift`, `SavedWorkView.swift`, `GlanceClient.swift` | Saved responses belong to the same store, retain their own timestamps and cannot fall through to network writes. |
| Timeline data and navigation | `WorkTimelineModel.swift`, `WorkTimelineMemory.swift`, `WorkTimelineViewport.swift`, `WorkTimelineFocus.swift` | Event identity, chronology, source and held snapshots remain authoritative. Selection is independent of the reading position. |
| Time canvas geometry | `WorkTimeCanvasLayout.swift` and tests | Packing covers all loaded dated records with time-anchored positions: panning translates cards without regrouping; every dated record is retained exactly once; cards do not overlap; large text reserves time-label space; a long task follows actual latest activity. |
| Native input | `WorkTimeCanvasInput.swift` and tests | Wheel input resizes the visible span over cards, canvas and overview; dragging pans; reserved OS modifiers pass through; input remains scoped to the canvas. |
| Timeline presentation and export | `WorkTimeCanvas.swift`, `WorkTimelineView.swift`, `WorkTimelineExport.swift` | Details appear below the timeline at selection; clusters stay inspectable in the same region; no inferred causality or execution duration; export retains identities and qualifications. |
| Information hierarchy | `ContextHelp.swift`, `ReadingSize.swift`, `Theme.swift`, `UsagePane.swift`, `UsageCapacity.swift` | Optional explanation is available by hover and keyboard; failures, cost basis, incomplete capture and install scope stay visible. |
| Distributable recorder | `packaging/validate-cli-payload.py`, `freeze-cli.sh`, `build-app.sh` | Framework aliases are validated in place — every symlink relative, resolving inside the frozen output, non-cyclic — and left intact so the framework stays a codesignable bundle. External, absolute, or cyclic aliases fail the build; the app's install-time payload validator binds each safe alias into the identity. |

The backend and native application are separate commits. Synthetic review
fixtures are included; frozen reviewer source copies and large image corpora
are local study artifacts, not production resources.

## Information placement

- Remove redundant labels and empty update counts. Show the arrivals action
  only when updates exist, keeping the live control and actions grouped at the right.
- Put supplemental explanation in an information icon with native hover help
  and a selectable popover reachable by keyboard. Use named disclosures for
  structured detail such as exact identities, capture proof and diagnostics.
- Keep current status and material qualifications visible. Setup's scope and
  possible manual merge steps remain beside Install. Usage keeps cost basis and
  partial subtotals beside monetary values.
- Display no comparison workspace or empty inspector. A selected record opens
  its details in the region below the timeline without changing the timeline's
  width or position.
  The overview pans and resizes the visible window; export and show-all time
  are available from Activity actions.
- Task status and activity take priority. The task navigator carries titles,
  outcomes, source and recency; usage, check history, sessions and coverage open
  as distinct categories in Task details.
- Current failures appear when present. Active filters show their scoped count
  and a clear action. Routine zero counts and refresh narration are omitted.
- Enlarged text reflows actions and evidence sections. Compact navigation keeps
  destination names in a picker instead of relying only on icons.

In the earlier information study, in the same 1120×860 synthetic timeline, the first evidence row moves from
vertical position 610 to 304: 306 points recovered. The control region contains
129 versus 46 OCR whitespace words in the final copy (43 in the frozen paired
candidate). The final copy adds the loaded-record qualifier beside the failure
count. This is rendered geometry, not measured
human comprehension or distraction. A separately preserved single-analyst
monitoring audit classifies incidental weighted units as 18/41 versus 2/18;
those classifications depend on the monitoring task and declared unit weights.

The earlier evaluation used 100 fresh evaluator agents: 50 baseline inspections and
50 masked paired comparisons across ten tasks and five contexts. The protocol
was fixed before review. Ordinal ratings, hard constraints, raw disagreements
and ten weight sensitivity variants are reported separately from machine tests.
Agents are not human participants, and preference between two candidates does
not establish a universal optimum. Final cohort results and post-freeze
dispositions are reported in the PR description. The complete sealed corpus is
retained locally in `design-plans/native-information-study/`; the content policy,
measurements and coverage audit are in
`design-plans/native-macos-overhaul/information-weight/`.

## Native review without installation

From the repository root:

```sh
swift build --package-path apps/agentacct -c release
apps/agentacct/.build/release/agentacct --native-review \
  design-plans/native-macos-overhaul/fixtures/live-progress.json
```

The scene picker includes setup, pending/failure/recovery, source health, saved
work, record details, and live sample progression. The fixture uses inert installer
callbacks and makes no live recorder requests. Use Reading at 100% and 185%,
including a 960×640 content window. A separate
`parallel-session-identity.json` fixture exercises similarly named session IDs.

## Verification and remaining limits

- The central timeline and fitted-popover suite reports465 tests, six guarded visual skips and
  zero failures. Three consecutive repeat-render checks passed after replacing
  unsupported ImageRenderer native-host placeholders with the same static
  SwiftUI content. A captured failure and amber-only negative control guard
  against those placeholders returning. Native interaction evidence is separate
  from these renderer checks. New regressions cover retired comparison bookmark
  keys, filtered export, clock warnings and authoritative Attention navigation.
  Existing cases retain partial-resolution, artifact-redaction, saved-timestamp,
  file-filter restoration and native scroll-position coverage.
- Python tests cover proposals for all four setup clients, reconnect ownership,
  session filtering before pagination, and timezone-independent timestamp bounds.
  Last backend suite: 2,852 passed; this Work refinement changes no Python source. Parent-process coverage is 88.05% of statements and
  78.63% of branches. The earlier native LLVM line coverage was 66.34%; this is not a coverage
  measurement of the later UI refinement. Standalone renders
  and native interaction checks are not merged into that unit-test profile.
- Fourteen new native renders cover the central timeline, selected detail,
  compact Work, enlarged text, offline and focused task views in light/dark.
  Seventy-eight earlier local PNGs cover the baseline counterparts plus enlarged
  Dashboard and Usage. Native interaction checks separately verify history
  restoration, keyboard setup, help access and export. Earlier comparison checks
  remain historical evidence for the removed feature.
- The local macOS renderer is 26.5.1; canonical pixel references require
  26.6 (25G72). The dedicated remote candidate workflow also failed its renderer
  guard: GitHub supplies 26.6.2 (25G83). Ordinary CI builds and review renders
  passed, but pixel-reference verification is not established. No references
  were promoted and the guard remains intact.
- Coverage identifies exercised code, not correctness. Swift UI action paths,
  full VoiceOver use, operating-system Login Items approval, and every lifecycle
  interruption are not exhaustively established by the local checks.
- Every interactive synthetic setup scene uses an inert installer and process
  runner, even inside a distributable app. A failure-Retry regression test
  verifies this boundary; preloaded screenshot state alone is insufficient.

Packaging requires a clean source commit and an embedded CLI carrying the same
provenance. Source edits alone do not update an installed app or recorder.
[PyInstaller preserves framework aliases](https://www.pyinstaller.org/en/v6.0.0/CHANGES.html),
and codesign needs a framework's canonical symlinks to treat it as a signable
bundle, so the installer accepts symlinks that are safe — relative and resolving
inside the payload — and binds each into the payload identity, rather than
rejecting all links. The freeze validates the aliases in place (relative,
in-payload, non-cyclic, not world-writable) and fails closed before signing; it
never dereferences them, because a symlink-free framework is not a valid signable
bundle. Regression fixtures include framework chains, outside targets, absolute
targets, cycles, broken links and special files.

## Shared timeline integration

The native Activity canvas now consumes the existing task timeline. Stable record IDs, session/section relationships, recorded time bounds, provenance, artifact redaction, supersession and dispositions are projected once in Python. Receipts and paged native history use the same events. Swift retains layout, gestures, filtering, selection and held/live state.

History loads from authenticated `/v1/task-timeline?task=…&limit=500`. Cursors are task-scoped immutable snapshots with a 120-second lifetime and an eight-snapshot LRU. Incoming evidence cannot shift records between pages. The native loader validates the complete sequence before publishing, retries expired cursors at most twice, and preserves retained history on failure. An unchanged first-page snapshot ID reuses the assembled local history. Only complete snapshots are saved for offline use. Requests are periodic snapshots, not a lossless event stream.

Review in this order:
1. Shared contract: `task_timeline.py`, the small `task_intelligence.py`/`receipt.py`/`api.py` adapters, and `test_task_timeline.py`.
2. Native consumption: `TaskTimeline.swift`, `WorkTimelineView.swift`, and the deleted session/check reconstruction in `WorkTimelineModel.swift`. Geometry and pointer interactions remain the central-canvas implementation.
3. Upstream reuse: PR #192's Markdown renderer/tests and shared cost/category helpers; PR #158's independent Sources refresh. The current failed-refresh presentation already gives errors precedence over retained healthy state, consistent with #158/#172. The timestamp upper-bound fix duplicates #191 and remains a single implementation.
4. Synthetic fixtures: generated with the production Python projection by `generate-timeline-fixtures.py`; no native fallback rebuilds missing timeline records.

PR #172's broad copy and temporal policy is not merged wholesale: it changes app-wide usage/reset copy and source-health models beyond this data-contract integration. Its useful failed-refresh invariant is already retained and tested. Future integration should reconcile remaining copy changes against this branch rather than reinstall its older Work view. PR #190 remains a draft; no branch was merged to main.

## Rendering performance

Ordinary window updates previously revalidated entire packaged and installed CLI
payloads on the main thread. An installed-app interaction sample attributed
7,805 of 9,256 main-thread samples to `MainWindow.body`; a 360-event horizontal
scroll probe took 38.2 seconds including synchronous accessibility requests.
These are diagnostic observations on one Mac, not frame-rate measurements.

Views now read a stored setup presentation refreshed after startup synchronization,
setup and reconnect, and when opening setup. The snapshot is display advice only:
protected operations still validate current ownership, content and provenance
before acting. Tests remove a previously accepted wrapper and verify that the
stale display cannot authorize reconnect. Explicit verification still runs during
these operations; this change removes verification from rendering.

Timeline filtering resolves its time window once per pass, instead of rescanning
all timestamps for every record. Viewport changes no longer rebuild the canonical
record projection, and unchanged poll responses do not republish it.

A second profile found saved-work JSON decoding in the window initializer. That
load now runs once per window in a background task; opening recovery still reads
a fresh copy. Immutable timeline snapshots retain their time bounds and newest
record, and pending-arrival differences update only when snapshots change. Tests
cover cache invalidation through held, arrivals, history and live transitions.

Navigation preferences save after a 250 ms pause and flush immediately when
leaving the timeline. Wheel ticks no longer write UserDefaults continuously or
invalidate the app's unrelated AppStorage consumers. Evidence recording and
refresh cadence are unchanged.
