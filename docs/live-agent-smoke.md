# Live Claude Code and Codex Smoke Tests

agentacct includes optional live smoke tests for maintainers who have Claude Code and Codex installed locally.

These tests are intentionally small. They prove that agentacct can launch a real agent process, capture its logs and metadata, and verify a deterministic marker in the output.

They are not part of default CI because they can consume paid model tokens.

## What this proves

A passing live smoke test proves this narrow claim:

```text
agentacct can launch and capture minimal Claude Code and Codex runs through `agentacct run`.
```

It proves that:

- the real `claude` or `codex` executable starts
- agentacct owns the child process it launched
- agentacct writes `metadata.json`, `stdout.log`, `stderr.log`, and `report.md`
- the expected marker appears in `stdout.log`
- the run exits successfully
- the smoke state can live in an isolated temporary directory

## What this does not prove

Do not use live smoke tests to claim that agentacct:

- automatically tracks Claude Code or Codex sessions launched outside agentacct
- reads exact Claude Code or Codex subscription billing
- fully automates every MCP/onboarding path
- provides production-ready dashboard UX for long real-agent sessions

Use careful public wording:

```text
agentacct wraps commands it launches and provides project-local onboarding helpers for coding-agent workflows.
```

Avoid wording like:

```text
agentacct automatically monitors Claude Code/Codex sessions.
agentacct automatically tracks all Claude Code/Codex usage.
```

## Run the smoke tests

Run both agents:

```bash
agentacct smoke all --json
```

Run one agent:

```bash
agentacct smoke claude-code --json
agentacct smoke codex --json
```

Use explicit isolated directories when you want to preserve evidence:

```bash
SMOKE_ROOT=/tmp/agentacct-live-smoke
mkdir -p "$SMOKE_ROOT/state" "$SMOKE_ROOT/work"

agentacct smoke all \
  --store-dir "$SMOKE_ROOT/state" \
  --work-dir "$SMOKE_ROOT/work" \
  --json
```

## Equivalent explicit commands

The smoke command is a convenience wrapper around agentacct-owned runs.

Claude Code:

```bash
agentacct run --store-dir /tmp/sentinel-claude-smoke/state --max-runtime 90s --on-timeout kill -- \
  claude -p 'Reply with exactly: AGENT_CHRONICLE_CLAUDE_WRAP_OK'
```

Codex:

```bash
agentacct run --store-dir /tmp/sentinel-codex-smoke/state --max-runtime 120s --on-timeout kill -- \
  codex exec --sandbox read-only --ephemeral --skip-git-repo-check \
  'Reply with exactly: AGENT_CHRONICLE_CODEX_WRAP_OK'
```

Do not add undocumented Claude flags to public smoke commands. agentacct's `--max-runtime ... --on-timeout kill` guardrail is enough for this release gate.

## Expected JSON fields

`agentacct smoke all --json` returns an array. Each item includes:

```text
agent
command
run_id
status
exit_code
reason
duration_seconds
run_dir
work_dir
expected_marker
marker_found
metadata_ok
stdout_log
stderr_log
report_md
metadata_json
```

A passing item should have:

```text
status: completed
exit_code: 0
marker_found: true
metadata_ok: true
```

`metadata_ok: true` means the smoke harness verified that `metadata.json` recorded agentacct ownership, successful completion, exit code 0, and the expected run ID.

## Token and cost expectations

These are real agent calls. They may consume paid tokens.

The prompts are deliberately tiny and deterministic. A normal release-gate run should be small compared with a real coding task, but exact token use depends on the installed agent, model, account, and local configuration.

Recommended practice:

- run only when a maintainer explicitly approves paid live testing
- use `agentacct smoke all --json` rather than long free-form prompts
- do not run live smoke in default GitHub Actions CI
- keep any release-gate token budget small and explicit

## Troubleshooting

### `Missing required executable on PATH: claude`

Install Claude Code or ensure `claude` is on `PATH`.

### `Missing required executable on PATH: codex`

Install Codex CLI or ensure `codex` is on `PATH`.

### Auth or account errors

Run the agent directly first:

```bash
claude -p 'Reply with exactly: ok'
codex exec --sandbox read-only --ephemeral --skip-git-repo-check 'Reply with exactly: ok'
```

If direct agent calls fail, fix the agent's own auth/config before testing agentacct.

### Timeout or killed run

The smoke harness uses short bounded runs. If a run times out:

- check the `stderr_log` path in the JSON output
- run the agent directly with the same prompt
- confirm the agent is not waiting for interactive login or workspace trust

### `marker_found: false`

The agent ran but did not print the expected marker. Inspect `stdout_log` and `stderr_log`. This usually means the agent added extra text, failed authentication, or hit a local configuration issue.

### `metadata_ok: false`

agentacct wrote artifacts, but the metadata did not prove ownership and successful completion. Inspect `metadata_json`; this is a release-gate failure until understood.

## Latest sanitized result

Record each maintainer release-gate run as a sanitized local note: no local temp paths, process IDs, agent session IDs, account identifiers, or raw credential-bearing logs.

## Release checklist

Before expanding public Claude Code/Codex claims:

- `agentacct smoke all --json` passes for a maintainer with both agents installed
- full test suite passes
- changed-file secret scan is clean
- docs still say agentacct does not automatically monitor sessions launched outside agentacct
- README links to this guide
