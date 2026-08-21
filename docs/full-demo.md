# Full Flow Demo

For the shortest no-key demo, run:

```bash
agentacct demo
```

That command creates a local agentacct-owned run, writes report/evidence artifacts, and prints follow-up report/dashboard commands. Without `--store-dir` (or `AGENTACCT_STORE_DIR`), the demo always runs in a throwaway temporary store and says so — even from inside an initialized project. Pass `--store-dir .agent-sentinel/state` after `init` to keep demo runs. The longer walkthrough below shows the individual primitives behind that flow.

This demo validates agentacct's current product loop without touching real
Hermes, Claude Code, Codex, or other existing agent processes.

It exercises:

- agentacct-owned command execution
- JSON reports
- local HTTP API
- MCP tools over stdio
- objective machine-check evidence

The demo task is deterministic and safe: it prints progress, sleeps, and exits 0.

## 1. Install locally

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest
```

## 2. Run a short smoke demo

Use a temporary store so the demo does not mix with your normal agentacct state.

```bash
export STORE_DIR=$(mktemp -d)

agentacct run \
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
agentacct report "$RUN_ID" --store-dir "$STORE_DIR" --json
```

## 3. Record objective machine-check evidence

This records a before/after signal such as "tests failed before, passed after".
It does not call any paid API.

```bash
agentacct outcome record-machine-check "$RUN_ID" \
  --store-dir "$STORE_DIR" \
  --name demo-check \
  --before-exit-code 1 \
  --after-exit-code 0 \
  --before-summary "demo check failed before the run" \
  --after-summary "demo check passed after the run"
```

## 4. Verify the local API

The local API binds to `127.0.0.1` by default.

```bash
agentacct api serve --store-dir "$STORE_DIR" --host 127.0.0.1 --port 8789
```

In another terminal:

```bash
curl http://127.0.0.1:8789/health
curl http://127.0.0.1:8789/runs
curl http://127.0.0.1:8789/runs/$RUN_ID/report
```

## 5. Verify MCP tools

The MCP server exposes safe local tools only.

```bash
agentacct mcp serve --store-dir "$STORE_DIR"
```

Current tools:

- `agentacct_list_runs`
- `agentacct_get_report`
- `agentacct_record_event`
- `agentacct_record_section`
- `agentacct_list_events`
- `agentacct_get_event_summary`
- `agentacct_record_machine_check`

## 6. Longer 10-minute-style run

For a longer validation similar to a real agent session:

```bash
export STORE_DIR=$(mktemp -d)

agentacct run \
  --store-dir "$STORE_DIR" \
  -- python examples/full_demo_task.py --steps 18 --sleep-seconds 30
```

That runs for about 9 minutes before report/API/MCP follow-up steps.
