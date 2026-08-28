# Usage and limits redesign: 100-scenario review matrix

Each numbered scenario has its own recorded review under
`reviews/review-NNN.md`. The runtime reached its cumulative agent-thread limit
after 16 distinct scenario reviewers had contributed, so three of those
reviewers simulated the remaining bounded blocks. The matrix is therefore 100
scenario probes, not a claim that 100 agent identities ran. The shared
candidate is a single **Usage & limits** pane that removes
the plan-percent/dollar mode fork, combines current capacity with ranged
consumption by client, keeps the daily trend and model breakdown, and moves
calibration, stale readings, and window definitions into explicit disclosure.

## People and first impressions

1. Expert, single heavily used Codex account; decide whether to keep working in 10 seconds.
2. First-timer with Codex, Claude Code, and Hermes in mixed reporting states.
3. VoiceOver-first operator comparing live headroom with today's spend.
4. Keyboard-only power user who never uses the pointer.
5. Team lead scanning multiple clients for the one that needs attention.
6. Cost-conscious solo developer who primarily cares about estimated dollars.
7. Subscription user who primarily cares about quota headroom and reset time.
8. Analyst who wants precise provenance and non-fabricated missing values.
9. User who assumes displayed cost is a provider invoice unless corrected.
10. User who assumes a provider limit is a hard budget unless corrected.

## Window, layout, and appearance

11. Minimum supported window at 960 by 560.
12. Large desktop window at 1600 by 1000.
13. Narrow navigation fit after removing the separate Limits tab.
14. Light appearance with low-contrast card boundaries.
15. Dark appearance with saturated threshold colors.
16. Increased Contrast enabled.
17. Reduce Transparency enabled.
18. Reduce Motion enabled during navigation and range changes.
19. Large accessibility text causing labels and KPI values to wrap.
20. Long localized labels and right-to-left reading order.

## Empty and disconnected states

21. No recorded usage and no client limit readings.
22. Recorded usage exists but no client reports limits.
23. Limits exist but the usage summary has not loaded.
24. Every limit reading is stale and stale rows are hidden.
25. Connecting to the daemon on first open.
26. Daemon disconnected while cached usage is visible.
27. Incompatible daemon schema.
28. Usage range request fails while live limit data remains valid.
29. Glance refresh fails while ranged usage remains valid.
30. First launch before recording setup is complete.

## Limit and quota shapes

31. One live weekly 7-day window.
32. One live 5-hour rolling window with no weekly window.
33. Both 5-hour and 7-day windows for one client.
34. Provider reports an unfamiliar custom window kind.
35. Window has no used-percent value.
36. Window has no reset timestamp.
37. Reset happens later today.
38. Reset happens within the next six days.
39. Reset is more than a week away.
40. Reset timestamp has already passed.
41. Used percent is exactly zero.
42. Used percent crosses the 75-percent attention threshold.
43. Used percent crosses the 90-percent attention threshold.
44. Used percent reaches or exceeds 100 percent.
45. Provider supplies an out-of-range negative percentage.
46. Fresh and stale readings coexist for the same client.
47. Multiple stale accounts are disclosed and toggled on.
48. Duplicate client limit entries arrive with different windows.
49. Limit client name is missing.
50. One hundred limit-reporting clients stress ordering and rendering.

## Usage and cost truth

51. Fresh tokens and sessions exist but no priced usage exists.
52. Partial known-additive cost must retain the tilde grammar.
53. Complete client-reported cost may use a bare dollar figure.
54. Complete pricing-table estimate must retain the approximation marker.
55. Cost exists while token count is absent.
56. Tokens exist while session count is absent.
57. Cache-read tokens dwarf fresh tokens.
58. A single tiny usage row tests compact-number formatting.
59. Billion-scale token totals test layout and precision.
60. Usage bucket has neither client nor model identity.
61. One hundred usage clients stress joined-row ordering.
62. One client has a very long generated identifier.
63. Client and model names contain Unicode and emoji.
64. Hundreds of models make the model breakdown unwieldy.
65. Period has missing cost but should not render as zero.
66. Period has missing tokens but should not render as zero.

## Range and refresh behavior

67. Seven-day range is the default decision window.
68. Thirty-day range changes totals, trend, and breakdown together.
69. Ninety-day range produces dense daily bars.
70. User switches 7 to 30 to 90 rapidly; only newest response may win.
71. One of the paired usage/plan requests fails during a range change.
72. Minute refresh begins while a range switch is in flight.
73. Data refreshes while the user is scrolled deep in the page.
74. Usage and limits carry visibly different freshness timestamps.
75. App relaunch should choose an understandable default, not a blank mode.

## Plan-share calibration

76. No client supports calibrated weekly plan share.
77. One client is calibrating with zero clean intervals.
78. One client can never calibrate because its rolling meter is incompatible.
79. One client is calibrated and supplies today and seven-day shares.
80. Multiple clients are calibrated.
81. Calibration state is absent from an older payload.
82. Calibrated client has no daily plan series.
83. Plan daily series has missing dates or sparse points.
84. Thirty- or ninety-day model share exceeds 100 percent of a weekly plan.
85. Some plan share comes from unusable timestamps.
86. Plan model shares contain missing percentages or token totals.

## Accessibility and interaction details

87. Threshold meaning cannot depend on color alone.
88. Limit meters need concise, complete VoiceOver labels.
89. Hover-only chart tooltips need a keyboard/screen-reader equivalent.
90. Segment controls and disclosures need stable focus order and identifiers.
91. Focus rings must remain visible over selected tab and card treatments.
92. Stale-reading toggle needs context and an announced result count.
93. Loading and refresh changes need status announcements without noise.
94. Pointer user scans the page without reading explanatory prose.

## Navigation, trust, and release safety

95. Dashboard's existing View Limits deep link must land in the merged pane.
96. Old internal `.limits` navigation must not leave stale task/session selections.
97. Menu-bar limit click-through should reach the same merged destination.
98. Deterministic snapshot mode must render controls without platform placeholders.
99. Light/dark reference images must preserve the same hierarchy and truth states.
100. Final regression review checks navigation count, docs, tests, stale code, and scope.
