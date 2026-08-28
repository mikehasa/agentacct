## Scenario

035 — Window has no used-percent value.

## Verdict

Named absence is required, not an empty meter.

## Findings

`HatchedTrack` and “used % unreported” already avoid fabricating zero. However, current layout omits reset text whenever percent is nil, even though reset is an independent provider fact. Unknown headroom also cannot participate in numeric sorting.

## Recommendation

Show the hatched track, “Used percent not reported,” and any valid reset together. Place the row in an unknown-headroom group after measurable live rows; never map nil to 0% or 100%.

## Test idea

Provide nil percent with a valid reset; verify reset remains visible, accessibility names the missing percent, and no numeric fill/order key is synthesized.
