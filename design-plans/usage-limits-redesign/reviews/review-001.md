# Review 001 — decision-first merged tab

## Recommendation

Replace the separate Usage and Limits navigation items with one **Usage & limits** pane. Remove the `plan % / $` mode switch: spend and headroom answer different parts of the same decision and should not hide each other.

## Above the fold

1. Keep a compact title/freshness header. Move the `7d / 30d / 90d` control out of the header and down to the history section so it cannot imply that it changes live limits or today's totals.
2. Lead with one **Now** card. For the heavily used Codex account, show `codex · pro`, then every live quota window side-by-side: window name, used percent, meter, and absolute reset. Order windows by urgency. Preserve named states such as stale, unreported percentage, or missing reset; do not infer “safe” or fabricate remaining units.
3. In the same card, place a clearly bounded **Today · all agents** strip with estimated cost and fresh tokens, including the existing confidence/source qualifier. This lets the operator compare headroom and today's spend in one scan.

For multiple accounts, keep the most urgent account visible first and provide a clear “N more accounts” disclosure immediately below; do not let secondary accounts push today’s spend off-screen.

## Secondary / progressive disclosure

Start a separate **History** section below the fold with the range picker, daily cost/tokens chart, then By client and By model tables. Show calibrated plan-share estimates as an optional subsection, not an alternate top-level mode; Codex can legitimately be `never` calibrated while provider-reported windows remain useful. Put stale-account inspection, no-limit cards, the calibration ledger, and window definitions behind explicit disclosures.

## Risk

Live limits/today totals come from `GlanceState`, while ranged usage and plan data refresh through `DashboardStore`. A merged surface can falsely imply one synchronized snapshot. Retain per-section freshness/error semantics and never carry an old value across a failed lane without marking it stale.
