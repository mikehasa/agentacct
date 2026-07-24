# Live Agent Smoke Results

This page records sanitized maintainer release-gate evidence for the optional real-agent smoke tests. These tests are intentionally not part of default CI because they launch real Claude Code and Codex binaries and may consume paid/subscription tokens.

## 2026-06-27 release-gate run

(Observed pre-rename: this run used the `agent-sentinel` binary and `AGENT_SENTINEL_*` markers, preserved below exactly as recorded. Dated evidence keeps the name under which it was observed.)

Command shape:

```bash
agent-sentinel smoke claude-code
agent-sentinel smoke codex
agent-sentinel smoke all --json
```

Preserved evidence command shape:

```bash
agent-sentinel smoke all \
  --store-dir "$SMOKE_ROOT/state" \
  --work-dir "$SMOKE_ROOT/work" \
  --json
```

Result summary:

- Claude Code: passed
  - status: `completed`
  - exit code: `0`
  - marker found: `true`
  - metadata ownership check: `true`
  - observed marker in `stdout.log`: `AGENT_SENTINEL_CLAUDE_WRAP_OK`
  - observed duration: about 3.81 seconds
- Codex: passed
  - status: `completed`
  - exit code: `0`
  - marker found: `true`
  - metadata ownership check: `true`
  - observed marker in `stdout.log`: `AGENT_SENTINEL_CODEX_WRAP_OK`
  - observed duration: about 12.41 seconds

Evidence checked locally:

- `metadata.json` existed for both runs.
- `metadata.json` recorded `owned_by_sentinel: true` for both runs.
- `metadata.json` recorded `status: completed` and `exit_code: 0` for both runs.
- `stdout.log` contained the expected deterministic marker for both runs.
- `stderr.log` existed for both runs.
- `report.md` existed for both runs.

Representative sanitized output:

```text
live smoke verified for: claude-code, codex
claude-code: completed, exit_code=0, marker_found=true, metadata_ok=true
codex: completed, exit_code=0, marker_found=true, metadata_ok=true
```

## What this result supports

This result supports the narrow public claim that Agent Chronicle can launch and capture minimal Claude Code and Codex runs that Chronicle itself starts.

Careful wording:

```text
Agent Chronicle wraps commands it launches and provides project-local onboarding helpers for coding-agent workflows.
```

## What this result does not support

Do not use this evidence to claim that Agent Chronicle:

- automatically monitors Claude Code or Codex sessions launched outside Chronicle
- reads exact Claude Code or Codex subscription billing
- tracks all ChatGPT subscription usage
- proves direct Anthropic or OpenAI API forwarding unless those provider smoke tests are run separately
- makes the local dashboard safe to expose publicly without authentication

## Artifact hygiene

The public evidence above intentionally omits machine-local temporary paths, process IDs, agent-run identifiers, and account-specific details. Keep raw smoke artifacts local unless there is a specific reason to publish them, and inspect them for secrets before sharing.
