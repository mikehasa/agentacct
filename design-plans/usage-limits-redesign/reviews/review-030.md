## Scenario

030 — First launch before recording setup is complete.

## Verdict

Needs an onboarding-aware empty state.

## Findings

First launch is not evidence of zero activity or unsupported provider limits. Although `MainWindow` can offer the setup sheet, a user who dismisses it would otherwise land on daemon-oriented empty copy with no clear next step.

## Recommendation

When recording is not configured, lead with “Set up recording to see usage and limits,” a short privacy/local-data explanation, and the existing setup action. Do not show empty KPIs or classify clients until setup completes.

## Test idea

Launch a packaged build with no recorder/store, dismiss setup, and verify the pane retains a working setup action and no false zero/unreported values.
