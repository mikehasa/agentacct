# Review 002 — first-time user, mixed client reporting

**Verdict:** Merge the panes and remove the plan%/$ fork. The proposed hierarchy works if capacity and consumption remain visibly different measures rather than adjacent numbers that imply a shared denominator.

**Reading order**

1. **Usage & limits** — “Provider-reported limits and recorded usage. These measures are not directly comparable.” Show separate freshness for limits and ranged usage if they differ.
2. **Current capacity** — lead with the only actionable reading: Codex, its rolling-window meter, percent available/used, and reset time. Do not aggregate clients here.
3. **Usage · Last 7 days** — tokens, sessions, estimated cost, and active days; the 7/30/90 control applies to this section and everything below, not current limits.
4. **By client** — one joined row per recording client, then daily trend, model breakdown, and collapsed **About these numbers** sections for learning state, stale readings, and window definitions.

**Exact row states/copy**

- **Codex** — badge **Live limit**; “`n% used · m% available`”; “5-hour rolling window · Resets today 14:40”; usage column “Last 7 days · `tokens` · `cost/unpriced`.”
- **Claude Code** — badge **Learning limit history**; “Not enough readings to estimate a weekly limit yet. Usage is still available.” Never show 0% or an empty meter.
- **Hermes** — badge **Usage only**; “This client does not report a provider limit.” Show its ranged usage normally, never 0% available/used.

**Accessibility issue:** The existing hatched track announces only “limit unreported,” which collapses Claude’s temporary learning state and Hermes’s unsupported state. Expose the distinct text state in each row’s VoiceOver label; pattern/color must be supplementary.

**Design risk:** A joined row can make a live rolling percentage look mathematically comparable with seven-day tokens or dollars. Preserve separate column headings, units, freshness, and a visible divider; never calculate a cross-client total, share, or “overall capacity.”
