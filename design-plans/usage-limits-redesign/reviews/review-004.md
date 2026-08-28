## Scenario

004 — Keyboard-only power user who never uses the pointer.

## Verdict

Needs revision. Merging the panes reduces navigation, and native segmented pickers, toggles, and disclosures can work well from the keyboard, but the candidate does not yet make the daily trend fully mouse-free.

## Findings

- Current range and series pickers use empty labels. They accept arrow-key changes once focused, but their purpose and focus position are not explicit.
- `UsageDailyChart` exposes labels yet selects a day only with `.onHover`; accessibility elements are not automatically keyboard focus targets. `PlanDailyChart` is also hover-only and lacks per-bar labels. A keyboard user cannot inspect exact daily values.
- Moving stale readings, calibration, and definitions into disclosures adds several focus stops. Their order, expanded-state announcement, and behavior during async refresh are unspecified.

## Recommendation

Use native labeled `Picker`, `Toggle`, and `DisclosureGroup` controls in a deterministic order. Make the trend one keyboard focus target with left/right-arrow day navigation and a persistent selected-day value; keep its focus stable when data refreshes. Ensure every disclosure toggles with Space/Return and visibly retains focus.

## Test idea

Disconnect the pointer and enable macOS keyboard navigation. Tab from the sidebar through range, trend metric, client filter, stale toggle, and each disclosure; operate them using arrows, Space, and Return. Confirm every focus ring is visible, every day value is reachable, and a refresh neither loses focus nor collapses user-opened sections.
