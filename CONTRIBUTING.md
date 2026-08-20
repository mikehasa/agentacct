# Contributing to agentacct

Thanks for helping improve agentacct. The project is early alpha, so the most useful contributions are small, well-tested improvements to local developer workflows.

## Product scope

agentacct is currently focused on being local-first Agent Work Intelligence for coding-agent runs: it records real token usage and the work a session actually did, and joins them honestly. Run-safety features (process ownership, budgets, checkpoints) are secondary and observe-only by default.

Good contribution areas:

- CLI usability
- local run/report evidence
- dashboard/API clarity
- MCP and project-local onboarding helpers
- safe provider-cost estimation and budget gates
- docs that make install, first run, and safety boundaries clearer
- tests for edge cases and safety regressions

Please avoid public contributions that add business roadmap, fundraising, enterprise sales, competitor analysis, or private strategy language to repo docs. Keep public docs focused on install, use, safety, limitations, integrations, and developer feedback.

## Safety principles

Changes should preserve these defaults:

- Local-first: state stays on the user's machine unless they explicitly export or forward it.
- No telemetry: do not add phone-home behavior.
- No stored API keys: provider keys should come from environment variables for commands that explicitly need them.
- Observe-only by default: basic setup should not require provider keys or paid API calls.
- Process ownership: agentacct should only control processes it starts and records as agentacct-owned.
- Localhost by default: dashboard/API services should not bind to public interfaces by default.
- Provider forwarding is opt-in and budget-gated.

If your change weakens any of these, call it out clearly in the PR and explain the mitigation.

## Development setup

The repository is currently private and unpublished. Obtain an authorized checkout from the owner; do not redistribute its location as a public install path.

```bash
cd /absolute/path/to/authorized/agentacct-checkout
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest
```

Run tests:

```bash
.venv/bin/python -m pytest tests/ -q --tb=short
```

Run a local smoke demo:

```bash
agentacct demo
```

## Before opening a PR

Please check:

```bash
git diff --check
.venv/bin/python -m pytest tests/ -q --tb=short
```

Also verify that changed files do not contain:

- API keys
- access tokens
- private logs
- private repository contents
- raw provider request/response bodies
- local absolute paths in shareable config examples

## PR guidance

Keep PRs small and reviewable. A good PR usually includes:

- clear summary
- focused code/docs change
- tests for behavior changes
- safety impact, if relevant
- exact validation commands run

For agent-assisted development, do not leave a meaningful, reviewable checkpoint only in a local branch. Once its relevant checks pass:

- commit the checkpoint intentionally;
- push the working branch to GitHub;
- create or update a draft PR immediately;
- link the branch, PR, commit, and validation result in the handoff.

Keep the PR in draft while the product is still being iterated. Do not merge it unless the owner explicitly asks. Throwaway experiments and broken intermediate states do not need to be published as separate versions.

For MCP tools, update all of these together:

- tool schema in `src/agentacct/mcp.py`
- handler in `SentinelMCPServer.call_tool`
- README MCP tool list
- tests in `tests/test_mcp.py`

For CLI behavior, preserve machine-readable `--json` output where it exists.

## Reporting security issues

Do not open public issues with vulnerability details, real API keys, private logs, or secrets. See `SECURITY.md` for the private reporting flow.
