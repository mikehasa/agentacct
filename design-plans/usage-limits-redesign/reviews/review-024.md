## Scenario

024 — Every limit reading is stale and stale rows are hidden.

## Verdict

Needs an above-fold stale explanation.

## Findings

Moving stale controls into a collapsed About disclosure can make Capacity now appear empty even while usage rows exist. The current pane correctly states that every reading is stale and reports the hidden count.

## Recommendation

Keep stale rows out of live-headroom ordering, but show “Capacity unavailable · N stale readings hidden” beside the section title with a direct reveal control. Revealed rows need stale labels and timestamps; they must not regain live styling.

## Test idea

Fixture only stale limits plus current usage; verify no live meters, visible hidden-count context, and correctly labeled rows after reveal.
