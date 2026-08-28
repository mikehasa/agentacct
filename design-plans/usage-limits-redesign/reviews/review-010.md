## Scenario

010 — A user assumes a provider limit is a hard budget unless corrected.

## Verdict

Revise before implementation.

## Findings

“Capacity now,” a filled meter, least-headroom ordering, and threshold colors/notches collectively resemble an enforceable budget control. “Provider-reported” names the source but never says what happens at 100%. The current pane compounds this with “Limits,” “quota,” and “above notify threshold”; its definitions explain rolling versus fixed windows, not non-enforcement, and the candidate moves them behind disclosure. Yet the design explicitly excludes hard-budget enforcement. Placing selected-range estimated cost beside the meter further invites the false inference that dollars consume or are capped by that quota. The mock’s bare “39%” also does not visibly say “used.”

## Recommendation

Put a persistent sentence directly under “Capacity now,” not inside “About these numbers”: “Provider-reported usage allowance; agentacct does not enforce a spending budget or stop work.” Render “39% used” and “provider reset,” and call the notches “attention markers,” never a notify threshold unless a notification actually exists. Keep estimated cost in its separately labeled lane and state that it is historical, not deducted from the allowance. Include the non-enforcement meaning in the section’s accessibility description.

## Test idea

Show a high-percent row with cost for five seconds, then ask what happens at 100% and whether a budget was configured. Pass only if users say the provider may restrict service and agentacct will not enforce spending; verify the sentence remains above the fold at 960×560.
