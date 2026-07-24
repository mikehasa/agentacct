# Task Intelligence and the local control plane

Status: implementation contract

Agent Chronicle is organized around one durable product object: the Task.
Client sessions, recorded sections, local usage, checks, artifacts, findings,
approvals, schedules, and execution attempts are records attached to that Task;
none of them becomes a second definition of the work.

```text
onboard -> first real Task -> understand the Task -> govern the next attempt
```

## Task identity

A recognized root client session creates the default observed Task boundary. A
user may explicitly link continuation chats when one task spans context windows.
Planned work receives a Task before an agent session exists. Public routes use
opaque Task ids and never embed raw client session ids.

Task state has three independent axes:

- execution: whether an attempt is queued, running, finished, cancelled, or lost;
- outcome: whether the target result is unknown, reported, verified, blocked, or
  has an open finding;
- control: whether Chronicle is ready, awaiting approval, holding on policy, or
  encountered a control failure.

A failed target-product check is an outcome finding. It is not automatically a
Chronicle failure or a user action.

## Evidence and usage boundaries

The Task detail page is a decision brief backed by an expandable evidence
record. It shows what was attempted, what changed, what proves the result,
usage/cost basis, unresolved findings, and a next action only when an owner was
explicitly recorded.

Local client logs remain the usage truth. MCP remains the richest source of
work meaning. Hooks, OTLP, connectors, CI, Git, and provider records keep their
own provenance and per-dimension authority. Missing or ambiguous joins stay
missing; Chronicle never allocates tokens to make a Task look complete.

## Activation runtime

`agent-chronicle onboard` composes existing project initialization, known local
source detection, project-local recording configuration, one local usage import,
and runtime startup. The managed runtime owns the dashboard and continuous usage
refresh with one absolute store. It survives the invoking shell and records a
process fingerprint so `status`, `stop`, and `repair` never signal an unknown
process.

Setup success and first-Task success are separate. Chronicle says it is waiting
until a real recognized client session appears after setup; demo rows never count.

The preferred project-local flow is:

```bash
agent-chronicle onboard
# Required: open a NEW recognized agent session in this project.
agent-chronicle status
```

MCP servers and hooks bind at client-session start, so the session that runs
onboarding cannot see the newly registered tools. The managed runtime is
idempotently controlled with `agent-chronicle start`, `status`, `stop`, and
`repair`; stop and repair never signal a process whose Chronicle ownership
proof no longer matches. No step requires provider API keys or a billing
connection. The older `init`, hook, import, watch, and `serve` commands remain
available as an advanced/manual fallback.

The default human and machine-readable Task detail routes are:

```text
http://127.0.0.1:8765/tasks/task_<opaque-id>
http://127.0.0.1:8765/api/tasks/task_<opaque-id>
```

## Owned execution boundary

The local control plane may launch and govern only executions Chronicle creates.
External Codex, Claude Code, Paperclip, OpenLIT, Entire, and provider processes
remain observed-only connectors.

The first complete control loop is:

```text
Task Contract -> policy preflight -> launch -> observe -> verify -> retry/cancel
```

An attempt freezes its objective, registered workspace, exact agent revision,
adapter/backend, permission envelope, budget basis, success checks,
git/worktree state, and environment key names. Commands are argv arrays, never
shell strings. If that agent registration changes after the attempt or its
approval is created, the old attempt fails closed and a new attempt must freeze
the new revision. Every process signal requires a matching PID birth time,
process group, cwd, executable, launch nonce, and Chronicle ownership record.
Unverifiable legacy processes are shown as lost and are never adopted.

Operational authority lives in a dedicated append-only Control Store. Evidence
v2 can support a policy decision, but evidence records cannot dispatch work.
Approvals are immutable, expiring, and single-use; mutations use idempotency
keys and expected revisions.

### Product and CLI workflow

The dashboard exposes this as a fourth top-level area, **Control**. Creating a
contract produces a pending attempt; it never starts a process. An observed
Task becomes a controllable Task only when the user explicitly selects it while
creating that contract. A planned Task receives the same opaque public id and
opens through the normal Task Intelligence route.

The minimal CLI setup is:

```bash
agent-chronicle control register-workspace \
  --store-dir .agent-sentinel/state --root . --workspace-id project
agent-chronicle control register-agent \
  --store-dir .agent-sentinel/state \
  --agent-id local-agent --display-name "Local agent" \
  --argv-json '["/absolute/command","arg"]'
agent-chronicle control plan \
  --store-dir .agent-sentinel/state \
  --objective "Run the bounded local task" \
  --workspace-id project --agent-id local-agent \
  --permission-envelope-json '{"mutation_mode":"read_only"}'
```

`control status` and `GET /api/control` return sanitized decision state. They
omit workspace/store paths, argv, PIDs and process groups, executable/cwd
fingerprints, manifests, ownership nonces, and raw process error strings.

An approval attached to an attempt follows one fail-closed order:

```text
ready -> awaiting_approval -> request -> approve -> consume once -> ready
                                 \-> reject -> policy_hold
```

The supervisor independently refuses to preflight any attempt whose control
state is not `ready`, whose registered agent revision changed, or whose adapter
is not Chronicle's `local_argv` + `subprocess` backend. A partial failure before
consumption therefore stays held; the caller never releases an attempt first
and tries to consume approval afterward.

### Runtime limits

- The owned supervisor is POSIX-only: it relies on `flock`, process groups,
  `fork`, and Unix signals.
- The web dashboard owns a persistent supervisor for its lifetime and recovers
  durable owned attempts on startup. A CLI `control launch` stays in the
  foreground until terminal; a one-shot CLI invocation never claims to be a
  background supervisor.
- After a process exits while no supervisor can observe its exit status,
  reconciliation reports `lost`; Chronicle never guesses success.
- Cancellation authority covers the exact owned process group. A child that
  intentionally detaches into another session is outside this controller's
  authority and is not adopted.

## Explicit non-goals

- hosted multi-tenancy, organization hierarchy, RBAC, SSO, or SCIM;
- kanban or a generic project-management product;
- a generic agent marketplace;
- mutating external orchestrators through read-only connectors;
- replacing provider billing portals or agent-native permission systems;
- storing full prompts, responses, thoughts, or transcripts by default.
