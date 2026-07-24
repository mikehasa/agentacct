# Prompt Budget Optimizer Concept

Prompt Budget Optimizer is a proposed preflight layer for Agent Chronicle.

The goal is simple:

```text
Spend fewer tokens before the agent run starts, without removing important constraints.
```

Agent Chronicle already focuses on runtime cost controls and post-run value evidence. Prompt Budget Optimizer would add a source-level savings layer before a run begins.

## Why this belongs in Agent Chronicle

Many expensive agent runs start with avoidable prompt/context waste:

- repeated instructions
- copied logs that include thousands of irrelevant lines
- vague goals that cause the agent to over-explore
- missing scope limits
- missing budget limits
- prompts that encourage broad refactors instead of small verifiable changes
- context pasted repeatedly across multiple turns

A runtime budget cutoff can stop runaway cost after it starts. A prompt budget optimizer can reduce the chance of runaway cost before it starts.

## What it should optimize for

The optimizer should reduce token use and scope creep while preserving intent.

It should help turn a loose request into a compact operating brief:

```text
Goal:
  What should be done?

Scope:
  What is in bounds?

Do not do:
  What should the agent avoid?

Constraints:
  Budget, safety, files, commands, provider limits.

Acceptance criteria:
  What proves the work is done?

Checkpoint:
  When should the agent stop and ask for review?
```

## Important: compression is not enough

A bad compressor can save tokens by deleting exactly the wrong thing.

Never drop:

- explicit user constraints
- budget limits
- security instructions
- secret-handling rules
- required author/identity settings
- files that must or must not be touched
- acceptance criteria
- rollback or verification requirements

The right product behavior is:

```text
compress cost, preserve control
```

## Scope-control examples

A useful optimizer should sometimes remind the agent to do less.

Instead of:

```text
Improve the whole repo and clean up anything you notice.
```

Prefer:

```text
Make the smallest change that satisfies the task.
Do not perform broad refactors.
Do not edit unrelated files.
Run the targeted tests first, then the full suite if needed.
Stop after one working implementation and report remaining ideas separately.
```

Instead of:

```text
Read all logs and figure out what's wrong.
```

Prefer:

```text
Inspect only the most recent failing log section first.
Extract the first actionable error.
Do not paste or reprocess the full log unless the first error is insufficient.
```

Instead of:

```text
Build the whole feature.
```

Prefer:

```text
Build a minimal verifiable slice.
Add one failing test first.
Implement only the code needed to pass that test.
Stop before adding optional polish.
```

## Possible CLI shape

Estimate only:

```bash
agent-chronicle prompt estimate prompt.txt
```

Preview a safer compact version:

```bash
agent-chronicle prompt optimize prompt.txt --preview
```

Use an optional model-backed optimizer with a hard budget:

```bash
agent-chronicle prompt optimize prompt.txt \
  --provider openrouter \
  --model openai/gpt-4o-mini \
  --max-total-usd 0.01 \
  --preview
```

## Possible output

```json
{
  "original_tokens": 8240,
  "optimized_tokens": 2930,
  "estimated_savings_percent": 64.4,
  "risk_level": "low",
  "preserved_constraints": [
    "do not push",
    "do not print secrets",
    "commit author must remain mikehasa",
    "run tests before reporting success"
  ],
  "warnings": [
    "Long logs were summarized; inspect original logs if the first error is insufficient."
  ]
}
```

## Product principle

Prompt Budget Optimizer should be preview-first.

It should show:

- original token estimate
- optimized token estimate
- expected savings
- preserved constraints
- removed/reduced sections
- risks or uncertainty

It should not silently replace a user's prompt in important workflows.

## Fit with Agent Chronicle

Agent Chronicle can become a three-stage cost/value system:

```text
Preflight:
  reduce prompt/context waste and scope creep

Runtime:
  enforce budgets, checkpoints, and cost cutoffs

Post-run:
  judge outcome evidence and cost-adjusted value
```

This keeps the product focused on Agent FinOps: not just spending less, but spending deliberately.
