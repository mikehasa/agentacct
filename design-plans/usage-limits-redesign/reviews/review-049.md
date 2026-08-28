## Scenario

049 — Limit client name is missing.

## Verdict

Needs revision.

## Findings

The current card says “unknown client,” while the merged design depends on client names for joining and identity. Treating that fallback as a real key could attach capacity to an unrelated “unknown” usage bucket or collapse several unnamed readings.

## Recommendation

Create a standalone “Client name not reported” capacity row with a stable non-name identity. Do not join it to usage, and keep multiple unnamed entries distinct unless the payload supplies another account key.

## Test idea

Provide two unnamed limits plus one unattributed usage bucket; assert three distinct states, no fabricated join, and complete accessibility labels.
