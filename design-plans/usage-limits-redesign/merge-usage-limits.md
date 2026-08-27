# Merge usage and limits into one decision-first pane

Written against: `7a0dc7e`

Status: final implementation decision after the 100-scenario review.

## Evidence chain

- Surface: `apps/agentacct/Sources/agentacct/UsagePane.swift`,
  `LimitsPane.swift`, `MainWindow.swift`, and the live app.
- Problem: users must switch tabs and remember historical usage while reading
  current provider capacity; the plan-percent mode can reduce the Usage pane to
  a small explanatory empty state.
- Design evidence: `Theme.swift` v10 truth rules, the existing single-series
  chart, current limit cards/meters, deterministic fixture data, dual-agent
  critique, and `scenario-matrix.md`.
- Owner: the main-window Usage destination and its pane composition.
- Scope and affected surfaces: navigation, Dashboard/menu limit destinations,
  usage/capacity presentation, Swift tests, visual snapshots, and app docs.
- Remaining validation risk: exact joined-row density still requires the
  deterministic minimum-window render and live keyboard/VoiceOver smoke test.

## Design decision

Use one navigation destination named **Usage** with a page title
**Usage & limits**. Remove the separate Limits tab and the page-level
`plan % / $` fork. Lead with a joined per-client **Current capacity** ledger
that pairs provider-reported windows and reset timing with that client's
selected-range consumption, but places them in separately labeled lanes so no
shared denominator is implied. Preserve the provider's `% used`; do not derive
`% remaining`. Follow with overall selected-range totals, the existing
single-series daily chart, model attribution, and an **About these numbers**
disclosure for calibration methodology and window definitions. Actionable
stale counts, unknown states, and calibration progress remain inline.

The ledger sorts valid fresh windows by highest reported use, then unknown
capacity by recorded volume and name. It preserves multiple/duplicate windows,
never treats nil as zero, and keeps capacity freshness/errors independent from
recorded-usage range state. Persistent copy states that provider limits are not
an agentacct-enforced budget and recorded costs are not invoices.

The signature is the capacity ledger: a quiet, instrument-like row for every
recording client, ordered by least live headroom, in which a provider meter and
honest absent state sit beside recorded activity. It is specific to agentacct's
job and replaces generic card stacking.

## Visual system

- Color, light/dark pairs: canvas `#F4F1E9 / #0D1215`; card
  `#FFFFFF / #1B252A`; ink `#171A1D / #F2F4F3`; muted
  `#59636B / #A5B0B4`; cobalt accent `#245BDB / #82A6FF`; threshold
  amber `#7A5A00 / #E7C66A` and coral `#B63F2F / #FF9B88` only when
  their existing semantic conditions apply.
- Type: existing Instrument Sans/system roles for prose and hierarchy;
  JetBrains Mono/SF Mono tabular roles for amounts, percentages, and times.
- Layout: preserve the 1172pt content measure, 24pt gutter/section rhythm,
  hairline division, maximum 4pt card radius, and no shadows or gradients.
- Aesthetic risk: retire the familiar pair of adaptive limit cards in favor of
  a full-width ledger. The meter remains the only emphasized graphic above the
  fold; the rest stays deliberately quiet.

## Layout

```text
┌ Usage & limits                                                ┐
│ provider-reported capacity · recorded local usage             │
├ Capacity now ───────────────────────────────────────────────────┤
│ CLIENT      PROVIDER WINDOW / RESET        SELECTED-RANGE USE  │
│ codex pro   Weekly  ███░ 39% · Fri 12:30   17.7M · ≈$310 · 20 │
│ claude      Learning limit history         5.1M · ≈$120 · 12  │
│ hermes      Limit not reported             145k · ≈$1 · 3     │
├ Recorded usage · range applies below             [7d 30d 90d] ┤
│ TOKENS        SESSIONS        EST. COST        ACTIVE DAYS      │
├ Daily activity                   Measure [Est. cost | Tokens] ─┤
│ proportional single-series bars                                 │
├ By model ───────────────────────────────────────────────────────┤
│ ranked model rows                                               │
├ ▸ About these numbers                                           │
└─────────────────────────────────────────────────────────────────┘
```

At the 960×560 minimum, the header, Capacity now heading, and at least the
first complete client row must remain visible. At accessibility text sizes,
the row stacks its capacity and consumption lanes without changing reading
order.

## Reuse

- `Theme`, `Type`, `Space`, `Metrics`, `Card`, `Chip`, `StripRow`,
  `MeterBar`, `LimitMeter`, `HatchedTrack`, and `UsageDailyChart`.
- Existing truth formatting: `Fmt.costDisplay`, `Fmt.costConfidenceLabel`,
  `UsageTotals.compact`, and absolute reset phrasing.
- Exemplar: the dashboard's `DashboardAgentPlanRow` joins per-client live
  limits, plan state, and seven-day usage without inventing data.
- New value type: one feature-local joined client row is justified because it
  centralizes ordering, named absence, and accessibility text for the merged
  surface; it must not become a daemon or global domain model.

## Changes

1. `MainWindow.swift` and `DashboardStore.swift`
   - Change: remove `.limits` from `MainPane`; route existing semantic
     `.limits` destinations to `.usage`; keep stale task/session clearing.
   - Preserve: dashboard/menu actions and the stable `navigation.usage`
     identifier.
   - Verify: four top-level tabs and all prior limit entry points open Usage.
2. `UsagePane.swift`
   - Change: compose the merged hierarchy, feature-local joined rows,
     capacity states, accessible summaries, and disclosures; remove the
     page-level mode fork while preserving calibrated plan facts in details.
   - Preserve: 7/30/90 atomic fetch behavior, cost grammar, single-series
     chart, model breakdown, no-fabrication states, and source freshness.
   - Verify: all scenario classes render an explicit state.
3. `LimitsPane.swift`
   - Change: move reusable limit meter/window presentation into the merged
     surface or feature-local components, then remove the orphan pane shell.
   - Preserve: threshold colors/notches, absolute reset text, stale toggle,
     calibration copy, and rolling/fixed definitions.
   - Verify: no duplicate or stale navigation/pane implementation remains.
4. Tests, fixture harness, references, and docs
   - Change: cover navigation routing, row joins/order, absent/stale/window
     variants, accessibility labels, range isolation, deterministic rendering,
     and updated user-facing navigation prose/images.
   - Preserve: canonical renderer rules and unrelated Dashboard/Work coverage.
   - Verify: functional suite, deterministic render, visual reference check,
     release build, live Computer Use inspection.

## Scope

- Inherit: dashboard and menu-bar click-throughs that already target Limits.
- Verify: minimum/reference widths, light/dark appearances, reduced motion,
  reduced transparency, keyboard focus, and live refresh/range behavior.
- Exclude: daemon/API/schema changes, notifications, hard-budget enforcement,
  saved view preferences, new dependencies, and unrelated dashboard redesign.

## Validation

- Product: answer current headroom/reset and selected-range spend without a tab
  switch; plan/calibration detail remains discoverable but not primary.
- Interface: populated, empty, usage-only, limit-only, stale, missing percent,
  missing reset, disconnected, range failure, long name, 960×560, and
  light/dark states.
- System: reuse existing semantic tokens/formatters; remove the parallel pane
  and do not introduce another global state owner.
- Repository: focused Swift tests, `swift test`, visual snapshot CLI/harness,
  `git diff --check origin/main`, and `apps/agentacct/Scripts/build-app.sh`
  must pass on the final tree.

## Stop conditions

- Stop if joined rows require re-deriving daemon truth, if a global range is
  found to alter provider windows, if dashboard/menu routing cannot retain its
  semantic limit destination, or if the minimum window cannot preserve one
  complete capacity row without hiding the page header.

## Design documentation

- After acceptance and validation: update `apps/agentacct/README.md`, root
  README screenshot/copy if the rendered surface changes, and the visual-test
  review matrix to describe the merged destination and its independent time
  lanes.
