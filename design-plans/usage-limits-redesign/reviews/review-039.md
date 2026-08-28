## Scenario

039 — Reset is more than a week away.

## Verdict

The distant-date format needs a year rule.

## Findings

Current output switches to month, day, and time after a week, but always omits the year. A far reset crossing December can therefore read as though it occurred in the current year.

## Recommendation

Show localized month/day/time for dates later this year and include the year when different from the current local year. Keep the absolute date in the provider lane; do not replace it with a coarse “in N days.”

## Test idea

Freeze time in December and render resets eight days away and in January; verify year inclusion, locale formatting, and a complete accessibility label.
