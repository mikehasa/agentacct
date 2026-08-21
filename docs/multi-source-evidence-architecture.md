# Multi-source evidence architecture

Status: implementation RFC
Version: 2
Last updated: 2026-07-23

## Decision

agentacct will present one evidence product while keeping a federated
implementation. agentacct remains the evidence and reconciliation kernel.
Mechanical client hooks, native client logs, OTLP, orchestrators, Git, CI, MCP,
and provider records are independent sources which make bounded claims.

MCP remains a first-class, high-value semantic source. It is not removed or
replaced. It also is not required for a client session, tool call, usage record,
or machine result to exist. When MCP is unavailable, the product must still show
the mechanically observed activity and explicitly mark work meaning as missing.

```text
native logs       host hooks       OTLP       orchestrator       Git / CI
     |                 |             |              |                |
     +-----------------+-------------+--------------+----------------+
                                       |
                              durable raw spool
                                       |
                              versioned adapters
                                       |
                         immutable EvidenceEnvelope v2
                                       |
                      source policy + identity reconciliation
                                       ^
                                       |
                           MCP / Work Event semantics
                                       |
                                       v
       Work Graph | Evidence Matrix | Discrepancies | Cost & Outcome Basis
```

This architecture follows four rules:

1. Raw evidence is append-only. A correction supersedes; it never overwrites.
2. Every source is authoritative only for specific dimensions.
3. A relationship is a claim with evidence, method, confidence, and conflicts.
4. Missing, partial, estimated, and conflicting are product states, not errors
   to hide with query-time coalescing.

## Product boundaries

### agentacct owns

- stable local Task identity and explicit continuation/merge history;
- immutable evidence envelopes and source provenance;
- deterministic deduplication and replay;
- identity and link claims with conservative refusal;
- cross-source discrepancies;
- cost, usage, outcome, and artifact basis labels;
- local-first privacy defaults;
- advisory policy signals derived from reconciled evidence;
- a local control plane for Tasks and execution processes agentacct itself
  launches, including attempts, approvals, budgets, schedules, and registered
  workspaces.

### Integrated systems keep owning

- Paperclip: its own task assignment, run control, approvals, budgets, and
  workspaces. The Paperclip connector remains read-only;
- OpenLIT and native telemetry: runtime observation and OTLP transport;
- Entire: Git checkpoints, artifacts, trails, and resume workflows;
- model providers: provider usage and billing records;
- CI systems: objective check execution and status;
- host agents: lifecycle hooks and local transcripts/rollouts.

agentacct does not take over or mutate their processes. Its local control plane
is not a Paperclip clone: agentacct controls only agentacct-owned executions
and projects their actions into the same Task/evidence model. It does not copy
multi-tenant organization charts, RBAC, generic project management, tracing
backends, code review workflows, or provider billing portals.

## EvidenceEnvelope v2 contract

Every source normalizes into a vendor-neutral envelope. The core schema never
adds `paperclip_*`, `openlit_*`, or `entire_*` fields.

Required identity and provenance:

- `evidence_id` and `idempotency_key`;
- `schema_version`, `source_system`, `source_type`, `source_instance_id`;
- `source_event_id`, `vendor_schema_version`, `adapter_version`;
- `event_kind`, `occurred_at`, `observed_at`, `ingested_at`;
- `raw_digest`, optional local `raw_ref`, and envelope `integrity_hash`.

Required epistemic state:

- `measurement_basis`;
- `completeness`: `complete`, `partial`, or `unknown`;
- optional `truncation_reason`;
- usage and cost confidence;
- capture level and redaction profile;
- sensitive-field presence declaration.

Subject references are sparse and may include organization, project, principal,
work item, execution, client session, turn, tool call, trace, span, artifact,
commit, pull request, machine check, provider account, or billing record.
An identifier is never global by spelling alone. Product projections namespace
it by its issuing system, organization/project scope, and (except for native
client-issued ids) source instance. Equal raw ids from Paperclip, OpenLIT, a
provider, or another project remain separate until a validated `ClaimedLink`
connects their evidence.

The envelope distinguishes facts from assertions:

- `observation`: a source mechanically measured or saw something;
- `claim`: a source or agent asserted meaning, ownership, completion, or cost;
- `derived`: agentacct produced a reversible projection from immutable inputs.

An adapter may not label an event `observation` when it only received a user or
agent assertion. The store rejects invalid combinations rather than silently
upgrading their trust.

## Link contract

Entity relationships are represented by `ClaimedLink`, not by an unqualified
foreign key:

```text
from_entity -> relation -> to_entity
asserted_by_evidence_id
join_method
confidence
candidate_count
conflict_state
valid_from / valid_to
```

Hard invariants:

- no unique strong evidence means no `exact` link;
- a conflict vetoes exact attribution;
- candidate count greater than one is ambiguous unless an independent exact key
  disambiguates it;
- an orchestrator can prove its own task-to-run assignment but not the complete
  provider cost of that run;
- a native usage log can prove session usage but not the business goal;
- Git proves that an object exists, not which prompt caused each line;
- MCP proves an agent-reported semantic statement, not provider billing.

## Source authority by dimension

`primary` means the strongest available direct basis for that dimension.
`corroborating` can support or conflict but cannot replace a primary source.
`claim-only` is rendered as a claim even when the source is internally trusted.

| Source | Lifecycle/activity | Work meaning | Usage | Cost | Artifact | Outcome | Control |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Native host hook | primary | unavailable | unavailable | unavailable | corroborating | corroborating when exit status is observed | unavailable |
| Native client log | primary | limited | primary client-reported | estimated or client-reported | corroborating | unavailable | unavailable |
| Native/provider OTLP | primary | limited | primary when provider/client measured | basis-specific | corroborating | corroborating | unavailable |
| MCP Work Event | corroborating | primary claim | debug-only unless independently measured | debug-only | claim-only | claim-only unless linked to machine evidence | unavailable |
| Paperclip | primary for its assignment | primary orchestrator claim | claim-only | claim-only | claim-only | claim-only | primary for its own runtime |
| Entire Git | unavailable | heuristic | unavailable | unavailable | primary Git object | corroborating | unavailable |
| CI check | corroborating | unavailable | unavailable | unavailable | corroborating | primary machine result | unavailable |
| Provider response | unavailable | unavailable | primary provider-reported | primary only when explicitly billed | unavailable | unavailable | unavailable |
| Provider invoice | unavailable | unavailable | aggregate/limited | primary billed | unavailable | unavailable | unavailable |
| agentacct wrapper | primary for owned process | unavailable | unavailable | unavailable | corroborating | primary exit result | primary only for owned process |

When two sources disagree about the same narrowly identified fact agentacct
preserves both envelopes and creates a discrepancy. A whole session or run is
not one immutable fact: sequential usage totals, changing lifecycle states,
and repeated checks are compared only when they share a request/span/tool fact,
an explicit measurement window, or the exact same event point. agentacct does
not choose a winner outside the declared dimension policy.

## Capture contract

Each adapter declares capabilities independently:

- session lifecycle;
- turns;
- tools;
- edits/artifacts;
- subagents;
- machine results;
- usage and cost;
- native OTLP;
- stable event identity;
- completeness and truncation reporting.

Hook execution follows a local-first hot-path contract:

1. parse a bounded JSON payload;
2. allowlist and redact metadata;
3. compute deterministic source identity;
4. append to the local durable spool;
5. return without rebuilding the dashboard or making a network request.

Capture is fail-open for the host. A broken agentacct adapter must not block the
developer's agent. The failure is recorded locally when possible and exposed by
doctor/status commands.

The first canonical mechanical events are:

- `session_observed`;
- `turn_observed`;
- `tool_observed`;
- `machine_check_observed`;
- `session_link_observed`.

Commands are machine checks only when an objective exit status exists and the
command category is recognized as test, build, lint, or typecheck. Text such as
"tests passed" is never parsed into a passed result without objective evidence.

## Work Event contract

The semantic model is transport-neutral. Existing `agentacct_*` MCP tools remain
compatible and write Work Events through the same normalization boundary as
HTTP or orchestrator imports.

A Work Event may express:

- objective and work-item identity;
- section start, checkpoint, completion, or blocker;
- decision, rationale, and next step;
- asserted artifact or outcome meaning;
- exact client/session/turn/message join keys when the host exposes them.

The transport is part of provenance. An HTTP event does not become MCP evidence,
and an MCP event does not become a mechanical observation. No generic writer can
mint trusted local-usage, hook-observed, CI, provider-billed, or Git provenance.

## Storage and projection

Evidence v2 is a shadow layer during migration:

- v1 `events.jsonl` remains unchanged and continues serving current features;
- v2 raw envelopes use a dedicated append-only spool;
- trusted refreshable usage transitions use a second append-only spool,
  `evidence-v2/refreshable-usage.jsonl`;
- a SQLite projection indexes evidence identity, source, subject, status, and
  discrepancy candidates;
- replay is idempotent and does not re-add envelopes;
- acknowledgement changes processing state, not immutable envelope content;
- derived views can be deleted and rebuilt from the spool;
- disabling v2 stops shadow writes without modifying v1 behavior.

High-volume hook or OTLP events must never be appended directly to the current
v1 ledger. Retention and future downsampling apply to raw activity, never to
provider billing or machine-outcome evidence without an explicit policy.

### Trusted refreshable usage is a current-fact lane

Local usage importers periodically replace cumulative facts in `events.jsonl`.
The v1 event id and server write time are transport details of that replacement,
not logical usage identity. Treating either as `source_event_id` for every
refresh turns an unchanged cumulative fact into unbounded raw Evidence versions.

Evidence v2 therefore has one narrowly gated reconciliation lane for
**trusted refreshable usage**. Only an allowlisted local usage importer may
enter this lane, and only for cumulative current-state rows whose provenance
and measurement basis pass source policy. A generic MCP, HTTP, connector, or
Work Event cannot opt itself into this authority by copying metadata.

The stable slot key contains all of:

- source namespace fingerprint (or an explicit unresolved sentinel that never
  coalesces with a resolved namespace);
- client;
- the client-gated normalized session id;
- importer-defined usage lane;
- representation class; and
- cumulative-update semantics.

Different clients, namespaces, sessions, lanes, representations, or additive
versus cumulative semantics are never folded together. The importer owns the
lane definition: when a client reports model-specific lanes, the model is part
of that lane; otherwise a provider/model change is a new revision of the same
slot rather than a new current total. This prevents both cross-lane merging and
double-counting a model switch. Product-facing synthetic run references retain
a readable bounded prefix plus a truncated 20-hex (80-bit) prefix of the slot
digest — enough that truncation or sanitization of the readable part cannot
accidentally merge otherwise distinct current facts. The complete slot digest
is bound on the evidence envelope itself (`source_event_id`/`raw_ref`), not in
the run reference; auditors comparing slots must use the envelope digest, not
the run-reference suffix.

Each candidate also has a normalized **truth digest**. It includes the usage
field-presence map and values, provider/model truth, usage confidence,
client-reported cost and its basis, normalization/additivity state,
hold/precedence state, and the exact supporting evidence references.
It excludes the reminted v1 event id, server-created/poll/ingest timestamps,
raw location, scan order, and price-derived estimated cost. Repricing therefore
does not manufacture a new client-usage fact, while changed client-reported
cost or changed evidence lineage does.

Transitions are ordered only by a source-native revision/update watermark,
never by the random v1 event id, agentacct scan time, or server write time. A
different revision with no comparable source order is retained as a conflict
rather than guessed into sequence:

- the same truth digest is a physical no-op while it remains the current head;
- if that head was tombstoned, a later complete snapshot containing the fact
  appends one ordered resurrection transition rather than leaving it deleted;
- a strictly newer different digest appends one immutable revision and
  supersedes the prior head;
- an older candidate is stale and cannot roll the head back;
- divergent content at the same order becomes one stable, fail-visible
  conflict; retries do not append more copies; and
- a tombstone is an ordered transition, not an in-place deletion.

Slot comparison, transition append, and head projection occur under the same
store lock, so concurrent identical refreshes create at most one transition
and a delayed older refresh cannot win. A tombstone may be inferred only from
a complete, successful, authoritative snapshot of the current trusted v1
ledger. A partial, limited, failed, or corrupt scan may project observed rows,
but absence in that scan is never deletion evidence.

After every successful persisted watcher tick, agentacct reconciles the full
current trusted usage slice in `events.jsonl`, not only rows rewritten during
that tick. This makes a prior fail-open Evidence shadow error self-healing on
the next good tick. Broken ticks retain the previous head and cannot create
tombstones.

The transition spool is separate from the generic `spool.jsonl` so a previous
binary never attempts to decode a new record kind. Each transition batch also
records the generic-spool byte fence observed under the shared lock, allowing a
compatible binary to replay both spools in the original cross-spool receipt
order. Generic EvidenceEnvelope behavior does
not change: its exact source identity still controls duplicate receipts and
same-identity/different-content conflicts, and it does not use refreshable-slot
coalescing.

For a clean rebuild, original Codex, Claude, and Hermes logs plus the trusted
`events.jsonl` ledger are the usage recovery inputs. Retained raw connector or
mechanical-capture inputs remain the recovery basis for their own evidence.
Neither the inflated refresh history nor the SQLite projection is promoted to
source truth merely because it is large; both old spools remain immutable
archives until an owner accepts the rebuilt store.

## Connector contracts

### Paperclip

The first connector is read-only. Exported/API snapshots map company, agent,
issue, task, run, work product, and reported cost into orchestrator claims.
`null` cost remains missing; it is never rendered as zero. agentacct sends no
pause, cancel, approval, or machine-check update by default.

### OpenLIT / OTLP

agentacct accepts OTLP JSON traces and maps only allowlisted resource/span
attributes. Prompt, response, thought, tool arguments, and tool results are
dropped by default. OpenLIT-specific fields are handled in the adapter, while
standard OpenTelemetry identity is preserved. Duplicate and out-of-order spans
are expected.

### Entire / Git

The connector reads public Git objects, known refs, and commit trailers. It does
not mutate refs or the worktree. Transcript ingestion has a separate opt-in and
is off. Line/prompt attribution is heuristic. A session with no code change is
not inferred to be complete merely because no checkpoint exists.

## Unified product views

The v2 product adds four evidence-specific views rather than cloning upstream
dashboards:

1. **Work Graph**: work item, execution, client session, trace, artifact, check,
   and billing relationships with link confidence.
2. **Evidence Matrix**: which source asserts each dimension, including missing
   and partial coverage.
3. **Discrepancies**: duplicate, conflict, missing-basis, and completion/outcome
   mismatches without automatic coalescing.
4. **Cost & Outcome Basis**: displayed numbers and statuses grouped by client
   reported, estimated, provider reported, provider billed, orchestrator claim,
   and objective machine evidence.

The v1 dashboard remains available during the shadow period. The v2 views read
the indexed projection and do not rebuild the entire event history per request.

### Primary product projection

The normal user journey is deliberately smaller than the evidence model:

1. **Work** leads with actionable reconciliation gaps, named work, recent agent
   activity, historical source coverage, and a compact usage snapshot.
2. **Usage** provides the complete saved usage explorer.
3. **Advanced** owns live local-log preview, normalized evidence projections,
   and JSON/record inspectors.

`/sessions` remains a stable Work explorer. `/raw`, `/work-graph`,
`/evidence-matrix`, `/discrepancies`, and `/cost-outcome-basis` remain stable
Advanced routes; compatibility does not require promoting them into primary
navigation.

The primary projection follows additional honesty rules:

- source evidence proves historical coverage, never current connector health;
- `connection_state=not_verified` is neither connected nor disconnected;
- agent/MCP work meaning remains a claim until linked mechanical evidence
  supports it;
- a trusted mechanical session observation may create an activity-only Task
  without MCP, but it carries no invented work title, token total, or cost;
- ambiguous usage is never allocated to make a work card look complete;
- opaque ids and normalized record metadata stay behind closed evidence drawers
  or Advanced inspectors;
- large record APIs use bounded cursor pages rather than unbounded default
  responses.

## Control bridge

Phase 6 is advisory by default. agentacct emits a structured `ControlSignal`
with its supporting evidence, basis, confidence, expiry, and recommended action.
Paperclip or another controller decides whether to act.

Hard enforcement is not eligible unless:

- the evidence basis is provider-billed; or
- a user explicitly approved a conservative basis and threshold; and
- every supporting evidence id resolves in the same local store, passes
  integrity/target/conflict validation, and is bound to this signal; and
- the target controller owns the execution; and
- the signal is fresh, non-conflicting, and idempotent.

agentacct never pauses or cancels an external run merely because an orchestrator
reported cost, an agent claimed completion, or an estimated price crossed a
threshold.

## Phase delivery and rollback

### Phase 0: contracts

Deliver this RFC, the privacy threat model, license BOM, and golden scenarios.
Rollback is documentation rejection; no runtime state changes.

### Phase 1: evidence shadow kernel

Deliver envelope/link models, source policy, v1 adapter, spool, projection,
dedupe, replay, and conflict preservation. Disable the feature to return to v1;
delete only derived projection data, never raw evidence.

### Phase 2: mechanical capture

Deliver Claude Code, Codex, and Cursor capability adapters and manifest
rendering. Installation/config mutation is separate and explicit. Disable the
capture adapter; current local import/MCP paths remain intact.

### Phase 3: transport-neutral Work Events

Route semantic events through a common normalization service while preserving
all public `agentacct_*` names and v1 writes. Disable shadow normalization to use
only the existing MCP/API path.

### Phase 4: read-only connectors

Deliver Paperclip snapshot, OpenLIT OTLP JSON, and Entire Git adapters. Each is
independently disabled and has no upstream write permission.

### Phase 5: evidence product

Deliver the four API/UI projections. A feature flag returns to the v1 dashboard;
derived views can be rebuilt from immutable evidence.

### Phase 6: advisory control and verification

Deliver advisory signals, refusal rules, replay/privacy/chaos/performance tests,
and local dogfood. External mutation remains disabled until a later explicit
authorization and product decision.

### Phase 7: product projection and progressive disclosure

Deliver the Work-first dashboard, actionable attention cards, human-readable
work summaries with closed evidence explainers, honest historical source
coverage, the Advanced inspection hub, and bounded evidence-event pagination.
Rollback is presentation-only: all stable legacy routes and immutable evidence
remain available, and no upstream connector or host configuration is changed.

### Phase 7.1: product home and action ownership

Replace the dashboard-style collection of attention, work, session, source,
and usage panels with one deduplicated Work feed. The product projection maps
work into `In progress`, `Blocked`, `Open finding`, `Verified`, `Agent
reported`, or `Activity observed`. Only an explicit blocker or recorded
user-owned next action is a user-facing action; a failed machine check remains
an open finding until the agent resolves it and is not assigned to the user.
Missing semantic context, join keys, usage
attribution, and source-health proof are integration or diagnostic states; they
remain in Sessions and Advanced and never masquerade as a user task.

For project-scoped stores, Work excludes activity carrying an explicit other-
project label while retaining older events whose project label is absent.
Usage and evidence stores are unchanged. This phase is a presentation and
scope-projection change, not a rewrite of ledger truth or immutable evidence.

### Phase 7.2: mechanical session observation bridge

Project accepted Evidence v2 client-hook observations into bounded saved
Session/Task activity. Exact client session and parent ids control folding;
namespace conflicts fail closed. Observed agent/model labels and recognized
machine checks may enrich the Task, while usage/cost lanes remain empty unless
a supported local usage importer supplies them. MCP continues to provide named
work meaning when it fires; the bridge does not synthesize it.

## Go/no-go invariants

- `false exact = 0` in the adversarial corpus;
- replaying a fixture 100 times creates one immutable envelope per source event;
- overlapping cost claims are not summed;
- `null`/unknown cost never becomes `$0.00`;
- metadata-only capture contains no prompt, response, thought, tool argument, or
  tool result text;
- MCP absence never means that work or usage did not exist;
- an adapter cannot grant itself authority outside source policy;
- disabling all v2 flags leaves v1 behavior and storage unchanged;
- connector disablement does not affect the upstream system;
- no integration introduces unknown or incompatible license material.

## Deliberate non-goals

- replacing MCP with hooks;
- copying OpenLIT's storage/dashboard platform;
- copying Paperclip's multi-tenant scheduler, RBAC, organization hierarchy, or
  generic project-management UI. agentacct may implement local approvals,
  registered workspaces, and bounded schedules for agentacct-owned attempts;
- copying Entire's resume, Trails, or transcript archive workflows;
- storing full prompts, responses, thoughts, transcripts, or tool bodies by
  default;
- treating telemetry volume as product value;
- inferring exact business ROI from activity alone.
