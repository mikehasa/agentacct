# Full Flow Demo

For the shortest no-key demo, run:

```bash
agent-chronicle demo
```

That command creates a local Chronicle-owned run, writes report/evidence/value artifacts, and prints follow-up report/dashboard commands. Without `--store-dir` (or `AGENT_CHRONICLE_STORE_DIR`), the demo always runs in a throwaway temporary store and says so — even from inside an initialized project. Pass `--store-dir .agent-sentinel/state` after `init` to keep demo runs. The longer walkthrough below shows the individual primitives behind that flow.

This demo validates Agent Chronicle's current product loop without touching real
Hermes, Claude Code, Codex, or other existing agent processes.

It exercises:

- Chronicle-owned command execution
- JSON reports
- local HTTP API
- MCP tools over stdio
- objective machine-check evidence
- isolated judge-package preparation
- optional OpenRouter judge scoring
- advisory cost-adjusted value scoring

The demo task is deterministic and safe: it prints progress, sleeps, and exits 0.

## 1. Install locally

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest
```

## 2. Run a short smoke demo

Use a temporary store so the demo does not mix with your normal Chronicle state.

```bash
export STORE_DIR=$(mktemp -d)

agent-chronicle run \
  --store-dir "$STORE_DIR" \
  -- python examples/full_demo_task.py --steps 6 --sleep-seconds 1
```

Get the run ID:

```bash
RUN_ID=$(python - <<'PY'
import json, os, pathlib
store = pathlib.Path(os.environ["STORE_DIR"])
runs = sorted((store / "runs").iterdir(), key=lambda p: p.stat().st_mtime)
print(runs[-1].name)
PY
)
echo "$RUN_ID"
```

Show the JSON report:

```bash
agent-chronicle report "$RUN_ID" --store-dir "$STORE_DIR" --json
```

## 3. Record objective machine-check evidence

This records a before/after signal such as "tests failed before, passed after".
It does not call any paid API.

```bash
agent-chronicle outcome record-machine-check "$RUN_ID" \
  --store-dir "$STORE_DIR" \
  --name demo-check \
  --before-exit-code 1 \
  --after-exit-code 0 \
  --before-summary "demo check failed before the run" \
  --after-summary "demo check passed after the run"
```

## 4. Prepare a judge package without spending money

This creates an isolated prompt/package for an LLM judge. It does not call the
LLM by itself.

```bash
agent-chronicle judge prepare "$RUN_ID" \
  --store-dir "$STORE_DIR" \
  --task-goal "Validate the Agent Chronicle full-flow demo." \
  --rubric "Score whether the run completed safely and produced useful evidence."
```

## 5. Optional: run the OpenRouter judge

This is the only paid step. Use a low-limit test key and a small hard budget.

```bash
export AGENT_CHRONICLE_OPENROUTER_API_KEY=<OPENROUTER_API_KEY>

agent-chronicle judge run "$RUN_ID" \
  --store-dir "$STORE_DIR" \
  --model openai/gpt-4o-mini \
  --max-total-usd 0.01 \
  --task-goal "Validate the Agent Chronicle full-flow demo." \
  --rubric "Score whether the run completed safely and produced useful evidence."
```

## 6. Compute advisory value score

```bash
agent-chronicle value compute "$RUN_ID" \
  --store-dir "$STORE_DIR" \
  --budget-usd 0.01 \
  --json
```

The value score is advisory. It combines:

- deliverable score from the judge
- machine-check evidence
- cost efficiency relative to your budget
- risk penalties for introduced failures

## 7. Verify the local API

The local API binds to `127.0.0.1` by default.

```bash
agent-chronicle api serve --store-dir "$STORE_DIR" --host 127.0.0.1 --port 8789
```

In another terminal:

```bash
curl http://127.0.0.1:8789/health
curl http://127.0.0.1:8789/runs
curl http://127.0.0.1:8789/runs/$RUN_ID/report
```

## 8. Verify MCP tools

The MCP server exposes safe local tools only. It does not expose a paid
`sentinel_run_judge` tool.

```bash
agent-chronicle mcp serve --store-dir "$STORE_DIR"
```

Current tools:

- `sentinel_list_runs`
- `sentinel_get_report`
- `sentinel_record_event`
- `sentinel_list_events`
- `sentinel_record_machine_check`
- `sentinel_prepare_judge`
- `sentinel_compute_value`

## 9. Longer 10-minute-style run

For a longer validation similar to a real agent session:

```bash
export STORE_DIR=$(mktemp -d)

agent-chronicle run \
  --store-dir "$STORE_DIR" \
  -- python examples/full_demo_task.py --steps 18 --sleep-seconds 30
```

That runs for about 9 minutes before report/API/MCP/judge/value follow-up steps.
