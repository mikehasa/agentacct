## Scenario

060 — Usage bucket has neither client nor model identity.

## Verdict

Needs revision.

## Findings

Current mappings label nil identities “unknown,” while `UsageBucket.id` is derived from missing client/model fields. Multiple anonymous buckets can therefore collide in `ForEach` or appear to be a real client that joins an “unknown” limit row.

## Recommendation

Use explicit “Unattributed client” and “Unattributed model” groups, aggregate like buckets deliberately, and assign stable non-display identities. Never join unattributed usage to unnamed capacity.

## Test idea

Load multiple identity-free buckets; assert stable rendering, no duplicate IDs, one deliberate aggregate per breakdown, and no capacity join.
