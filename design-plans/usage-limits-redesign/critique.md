# Usage and limits: current-surface critique

Written against: `7a0dc7e`

Method: dual-agent (`/root/critique_a` and `/root/critique_b`) plus live
Computer Use inspection of `/Applications/agentacct.app`.

## Design language

- Audited surface: `UsagePane`, `LimitsPane`, top-bar navigation, and their
  live light/dark rendered states.
- Design sources: `Theme.swift`, the v7 pane implementations, the deterministic
  dashboard fixture/harness, `apps/agentacct/README.md`, and the running app.
- Documented decisions: semantic color jobs; named absent states; one chart
  series at a time; tabular data typography; no shadows/gradients; client data
  is rendered without re-deriving facts.
- Governing owners and consumers: `MainWindow.swift`, `UsagePane.swift`,
  `LimitsPane.swift`, `DashboardStore.swift`, `GlanceState`, and dashboard/menu
  deep links.
- Explicit exceptions: snapshot mode replaces native segmented Pickers with
  deterministic chips.

## Design health

Nielsen total: **25/40 (acceptable)**. The product-specific truth grammar is
strong, but the composition is a category-generic KPI/chart/table dashboard
and the primary operational decision is split across two destinations.

## Findings

| # | Problem | Evidence | Proposed change | Scope | Confidence |
| --- | --- | --- | --- | --- | --- |
| 1 | A single decision requires cross-tab recall | Live Usage showed high cost/tokens while live Limits separately showed low quota use and reset time | Organize one pane around current capacity, then consumption drivers and attribution | Navigation, Usage pane, Limits content, deep links | High |
| 2 | Incomparable time windows can be conflated | Usage uses selectable 7/30/90-day history; providers report independent rolling/fixed windows; plan share has its own weekly denominator | Keep capacity and selected-range history in named semantic lanes; no global percent or implied shared denominator | Joined client presentation and copy | High |
| 3 | Assistive detail is incomplete | Source has hover-only plan bars without labels; grouped controls have empty labels; live tables flatten in the observed AX tree | Add contextual control labels, complete meter summaries, accessible chart summaries/marks, and stable identifiers | Merged pane and tests | High |
| 4 | Durable explanation occupies primary space while terse modes hide meaning | Limits permanently displays calibration/definitions; Usage exposes `$` and `plan %` and can become mostly blank | Remove the page-level mode fork and place calibration/definitions/stale data in explicit disclosures | Merged pane hierarchy | High |

## Improve first

Replace the two-tab information architecture with one decision-first surface.
This removes the memory bridge while preserving the independent meanings and
freshness of provider capacity and recorded consumption.

## Evidence limits

- The markup detector was not applicable to native SwiftUI and was recorded as
  unavailable, not as a clean scan.
- Browser DOM overlays were skipped because the target has no browser surface.
- Computer Use captured both AX trees and the Usage raster; source and AX
  evidence back the Limits layout after later runtime capture timed out.
- Full VoiceOver speech, keyboard traversal, and contrast sampling remain
  implementation validation tasks.
