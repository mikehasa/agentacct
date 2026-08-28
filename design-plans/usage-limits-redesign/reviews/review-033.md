## Scenario

033 — One client reports both five-hour and seven-day windows.

## Verdict

The row must preserve both windows.

## Findings

Sorting by least headroom needs one client-level key, but reducing the display to only the hottest window would hide a second constraint with a different reset. Stacking two full cards would defeat the compact ledger.

## Recommendation

Use two compact window subrows under one client identity. Derive ordering and the header status from the least-headroom live window, while retaining each label, percent, meter, and reset independently.

## Test idea

Set 5h to 92% and weekly to 20%, then reverse them; verify both remain visible and the client’s sort/status follows the riskier window.
