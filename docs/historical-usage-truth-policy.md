# Historical usage truth policy

Status: **approved**<br>
Policy ID: `historical_usage_reference_v1`<br>
Approved: 2026-07-20 (Asia/Singapore)

## Scope

This policy applies only to legacy usage rows whose historical source did not
record whether an input, output, reasoning, or cache component was actually
present. The manifest-verified real legacy snapshot contains 876 such Codex
usage lines. It does not apply to an explicit measured zero or to a component
whose source-presence bit is proven.

## Canonical truth

- An unproven component remains `NULL` with its `reported` flag false.
- A numeric compatibility value must never turn absence into zero or populate a
  canonical component field.
- An independently reported total may remain canonical total-only truth, but
  only when its exact representation and precedence basis are validated.
- Unproven component values do not contribute to canonical aggregates, cost or
  repricing calculations, migration acceptance, or cutover parity.

## Historical reference

If product continuity later requires the old numeric presentation, it may be
reconstructed only as a read-only projection with all of these properties:

- explicitly labeled `historical reference`, `non-canonical`, and `read-only`;
- derived from a manifest-verified snapshot or exact validated fallback basis;
- kept outside canonical component totals and all write paths;
- never written back, dual-written, or retained as a permanent shadow store;
- removable only under a separate approved compatibility-removal decision.

The current spike's parity oracle already proves the exact, narrow
`codex-sqlite-tokens-used-fallback-v1` compatibility predicate while comparing
canonical missingness independently.

## Audit disposition

The original parity report remains immutable evidence of what the importer saw
before this policy was approved. Its 2,628 component-presence
`requires_choice` issue instances remain visible in that report. Future dry-run
reports may disposition those exact reasons as `approved_visible_missingness`
under this policy ID; this is not a migration exclusion and does not authorize
dropping a row.

The separate 2,324 lines without a source namespace are not resolved by this
policy. Production migration and live cutover therefore remain **NO-GO**.

## Non-authorizations

This approval does not authorize live-store mutation, writer integration,
cutover, an adapter, a rename, Control or Dashboard redesign, shadow/dual-write,
commit, push, PR, or release work.
