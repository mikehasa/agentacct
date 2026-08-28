## Scenario

079 — Calibrated client supplies today and 7d shares.

## Verdict

Good fit for progressive disclosure.

## Findings

The existing plan view labels both fixed windows and keeps them independent of the selected model-breakdown range. Moving these facts below Capacity now reduces competition with provider headroom, provided neither value is discarded or relabeled as live quota.

## Recommendation

Keep “today” and “7d” together under the client’s calibration detail with “estimated % of weekly plan” and its basis.

## Test idea

Select 30d while today and 7d shares exist; assert those two values stay unchanged and the model breakdown alone changes to 30d.
