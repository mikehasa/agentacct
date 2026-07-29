# Canonical-Authoritative Writes — Design (P1 second cut)

Status: **design draft, 2026-07-24.** No code in this document is implemented yet;
this is the plan for the P1 second cut. Nothing here changes production. The
final authority flip is owner-gated and requires a time-based observation window
(soak) that cannot be compressed into one session.

This document is the answer to the open P1 task in `PROGRESS.md`:
> "Define reconciliation, quarantine, rollback, and disaster-recovery behavior
> using original client logs as the durable source. Run parity, exact-receipt
> checks where applicable, full tests, bounded live canaries, and an observation
> window."

## 0. What "authoritative" means, and why it is not a flip

Today canonical is a **fail-open shadow**: every write goes to the v1 `events.jsonl`
ledger first (under a cross-process lock), then a per-event canonical "shadow"
write runs off-lock and may fail without ever failing the v1 write. v1 is the
record; canonical is disposable evidence.

"Authoritative" means the inverse for the lanes' canonical models: the canonical
store becomes the primary write target and source of read truth, v1 becomes a
compatibility mirror, and a canonical write failure surfaces instead of being
swallowed. That inversion touches durability, concurrency, completeness, trust,
and recovery all at once, so it is sequenced as many small reversible increments
behind a tri-state flag, not a single switch. The flip itself only happens after
every precondition below is closed, a windowed live parity soak is clean, and the
owner approves — matching the locked principle that v1 leaves the hot path only
"after successful soak and confirmation."

## 1. Today's write architecture (verified map)

- **v1 ledger** — `events.jsonl`, owned solely by `SentinelService` (`service.py`).
  Single write funnel for CLI/HTTP/MCP. Serialized by an advisory cross-process
  `fcntl.flock` on `events.jsonl.lock`. Mostly append-only (`record_event`), with
  an atomic whole-file rewrite lane (`replace_events`, `os.replace`) that carries
  `dedup_key`/`replace_guard` preconditions for atomic multi-row usage revisions.
  Unparseable lines are preserved verbatim (torn-line forensics). A plain append
  is not fsynced; the finding-disposition writer and the Evidence-v2 spool are.
- **Canonical shadow** — `CanonicalLiveRuntime.shadow_v1_event` (`canonical_live.py`),
  per-event, one `BEGIN IMMEDIATE` per row, opened after the v1 commit and outside
  the v1 lock. Fail-open: a blanket `except` swallows any error into a counter.
  Idempotent by repository content hashing (`usage_content_hash` excludes ingestion
  provenance, so an unchanged re-observation is a physical no-op).
- **The "deferred" divergence** — the importer collapses a whole file to one
  last-wins row per session before one reconcile; the live writer sees events one
  at a time and has "no line number," so stale/tie/order-less/unseen-parent
  observations are **visibly deferred** (`*_deferred` dispositions) rather than
  written, trusting v1 as the record. The module docstring names replicating the
  importer's absorb semantics as a precondition "before any import may target a
  live store."
- **Write flag** — `AGENTACCT_CANONICAL_LIVE_WRITE`, binary today
  (`{"1","true","yes","on","shadow"}` all mean "on"). No authoritative branch
  exists anywhere. The read flag `AGENTACCT_CANONICAL_READ` is separate and
  independent, OFF in prod (this cut's Block 1 migrated the four HTML read surfaces
  behind it; still OFF).
- **Evidence v2** — a third, independent additive shadow store (`evidence-v2/`),
  fsynced spool as its own source of truth, with the refreshable-usage identity
  that makes unchanged refreshes physical no-ops and infers deletion only under
  the v1 lock over a fully-parsed ledger.
- **Durable recovery source = raw client logs** on the owner's machine (codex
  `~/.codex/sessions/**`, claude-code `<home>/projects/**`, hermes `state.db`,
  etc.). The rebuild machinery re-discovers those logs; it does not merely replay
  `events.jsonl`. v1 itself is derived for usage/session lanes but is the *only*
  home for some lanes (below).
- **Existing offline cutover toolkit** (`canonical/cutover.py`, `product_parity.py`,
  `read_canary.py`, `evidence_rebuild_*.py`): parity runner v2/v3 → `prepare_cutover`
  → `promote_candidate` (atomic `os.replace`, role flip candidate→live, adapter
  normalization, retained shadow backup, **pre-swap durable emergency receipt**) →
  `verify_promotion` → `rollback_promotion`. All **stop-the-world** (require every
  writer stopped, refuse a busy DB). The parity report hard-codes
  `cutover_gate_passed=False` / `cutover_decision="no-go"`: it certifies a
  core-truth-slice, **not** an operational live-write cutover. No generation is
  ever deleted.

## 2. Target architecture

- **Tri-state write mode:** `off | shadow | authoritative`, via a mode accessor
  (not today's bool). `off`/`shadow` behave exactly as now. `authoritative` makes
  the canonical write primary for modeled lanes and surfaces failures.
- **v1 as compatibility mirror:** under `authoritative`, v1 is still written (for
  rollback safety and for v1-only lanes) but is no longer the authority for modeled
  lanes. v1 leaves the hot path only later, after soak + owner approval.
- **Read coupling:** an authoritative posture serves reads from canonical (Block 1's
  read path), keeping the labeled fail-visible fallback. The two flags are combined
  only at the final cutover; until then they stay independent and OFF.

## 3. Preconditions (the risk register) — each must be closed before the flip

Each item lists the property v1 guarantees today that canonical does not yet, and
the increment that closes it (§5). None may be skipped; several are themselves
non-trivial.

1. **Absorb parity.** The per-event writer must replicate the importer's whole-file
   last-wins absorb (min-started/max-activity/last-wins kind+parent, incumbent
   parent identity), so `*_deferred` rows stop being "v1 still has it" and become
   correctly written. Otherwise authoritative silently drops deferred data. → I1.
2. **v1-only lanes.** Enumerate every `event_type` in `events.jsonl`. Lanes with no
   canonical model — finding dispositions (explicitly skipped today), MCP
   sections/work-claims beyond the persisted subset, machine-checks, budget/outcome
   notes, client-context attach provenance — must be modeled in canonical or given
   an explicit retention plan, or authoritative drops them. → I2.
3. **Single write serialization authority.** v1's `flock` serializes every writer
   across processes; canonical shadows run off-lock, one transaction per row.
   Authoritative needs one serialization authority spanning dashboard/watcher/MCP/CLI
   plus the atomic multi-row batch guarantee `replace_guard`/`dedup_key` give. → I3.
4. **Durability contract.** Define and test a canonical crash-durability contract
   (WAL checkpoint/fsync) at least as strong as "the row survives power loss once
   the write returned." → I3.
5. **Trust-marker/provenance gates.** The server centrally stamps/strips
   `trusted_usage_import`, `trusted_session_observation_import`, client-context
   provenance, finding-disposition provenance, etc. Authoritative canonical must
   reproduce the exact same server-authored gates so an HTTP/agent caller cannot
   forge trusted rows directly in canonical. → I2/I3.
6. **Namespace TOFU.** Late namespace binding rewrites historical v1 rows atomically
   and is deliberately not shadowed ("canonical source identity is immutable").
   Authoritative must define how late namespace resolution is expressed, or rows
   strand under the live-unresolved scheme. → I2.
7. **Complete-snapshot deletion authority.** Evidence-v2 infers deletion only under
   the v1 lock over a fully-parsed ledger. Canonical has only per-row shadowing —
   it cannot represent "current" including removals. Authoritative needs a
   lock-consistent complete reconcile. → I3.
8. **Torn-line forensics.** v1 preserves corrupt-but-recoverable lines and withholds
   "complete" authority when any exist. Canonical needs an equivalent "cannot prove
   completeness → do not infer deletion/tombstone" guard. → I3.
9. **Adapter/identity continuation.** `get_or_create_source` hard-fails on adapter
   mismatch and the legacy importer refuses live-role stores; continuation across
   import→live in one store is only reconciled at promotion by an `UPDATE`.
   Authoritative writing onto a rebuilt-from-logs store must resolve this seam. → I0/I3.
10. **Proven recovery equivalence.** Because raw client logs are the durable source,
    canonical must be provably rebuildable from raw logs to bag/receipt parity
    before authority moves — else a lane recoverable only via v1 (items 2/6) becomes
    permanently lossy. → I6.

## 4. Safety machinery (reconciliation / quarantine / rollback / DR / canary / window)

- **Continuous parity monitor.** Extend `product_parity` (today a one-shot offline
  bag-equality over the four core surfaces) into a windowed, read-only live
  comparison of canonical vs v1 truth, reusing the blocking-vs-non-blocking
  divergence classification (`same_slot_equal_order_link_drift` is non-blocking
  provenance drift; usage-material divergence stays blocking). Emits divergence
  counts over the window.
- **Reconciliation.** Divergences reconverge via the complete-snapshot canonical
  reconcile (I3) plus the existing `CanonicalRebuildPolicy` for read models. Nothing
  is silently corrected — every reconcile emits a receipt.
- **Quarantine.** Divergent/unprovable rows are held in the canonical conflict lane
  (`source_conflicts`), never dropped; `prepare_cutover` already refuses a candidate
  with any open conflict, so quarantine is a hard gate.
- **Bounded write canary + observation window.** Extend `read_canary` (before/after
  `/health` counter deltas with fallback/error guards) into a write canary that also
  bounds projection lag (`canonical_sequence − built_through_sequence`) and parity
  divergence over a defined window under authoritative write load. The observation
  window is dual-write (shadow) + continuous parity for a fixed period; divergence
  above threshold triggers auto-rollback.
- **Rollback / DR.** Authoritative rollback is cheap because v1 is still intact:
  flip the mode back to `shadow`, v1 remains the record, no data lost. For a promoted
  store, reuse `cutover.py` `rollback_promotion` (atomic `os.replace` back to the
  retained backup, refuses if the promoted store drifted from its receipt, pre-swap
  emergency receipt for crash-DR). Ultimate DR is rebuild-from-raw-logs
  (`legacy_recovery.py`), never deleting a generation.
- **New authoritative-cutover gate.** A new parity schema version / decision above
  the withheld core-truth-slice gate, certifying *operational* readiness: all
  preconditions closed + windowed live parity clean + write-canary green + recovery
  proven. This is the gate the current `cutover_decision="no-go"` deliberately
  withholds.

## 5. Increment sequence (each reversible, flag-gated, tested; no prod flip)

- **I0 — tri-state flag + mode accessor.** Replace the bool with `off|shadow|authoritative`;
  default unchanged (prod stays `shadow`). The `authoritative` branch initially
  raises "unsupported until preconditions closed." Resolve the adapter-continuation
  seam (item 9) at this layer. Foundational, low-risk.
- **I1 — absorb parity in the live writer.** Kill the `*_deferred` divergence;
  replicate the importer's absorb. Parity tests proving per-event and whole-file
  paths converge. Improves shadow fidelity even before any flip (makes parity able
  to pass).
- **I2 — v1-only lane inventory + model-or-retain.** Enumerate every `event_type`;
  model findings/MCP-sections/machine-checks/budget-notes/client-context in canonical
  or record an explicit retention decision. Reproduce trust-marker gates. Namespace
  TOFU representation.
- **I3 — complete-snapshot canonical reconcile + serialization + durability.**
  Lock-consistent complete reconcile (deletion authority + torn-line guard), a single
  cross-process write serialization authority, and a tested WAL durability contract.
- **I4 — continuous parity monitor (read-only) + health surfacing.** Windowed live
  canonical-vs-v1 divergence with blocking/non-blocking classification.
- **I5 — write canary + observation-window harness + auto-rollback wiring.**
- **I6 — the new authoritative-cutover gate** (parity schema vN) certifying operational
  readiness + proven recovery-from-logs equivalence.
- **I7 — owner-gated flip, after soak.** Enable `authoritative` in a bounded canary,
  observe the window, then full. Then (later, separately) archive v1 and request
  approval before it leaves the hot path.

## 6. Owner-gated / never-without-approval

Push, runtime restart, any prod flag flip (read cutover or write authority), the
final authority flip, and removing v1 from the hot path. The observation window is
mandatory and time-based. Never run rebuild/activate against the archived old store.

## 7. Non-goals

Removing v1 from the hot path this cut; rewrites; changing the locked truth/attribution
principles. Those are out of scope for the P1 second cut.
