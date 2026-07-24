"""A deterministic task for Agent Chronicle end-to-end demos.

This script is intentionally boring: it sleeps, prints progress, and exits 0.
Use it to validate Chronicle's run/report/API/MCP/outcome/value pipeline without
calling a real agent or touching real user processes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a safe deterministic Agent Chronicle demo task.")
    parser.add_argument("--steps", type=int, default=6, help="Number of visible task steps to print.")
    parser.add_argument("--sleep-seconds", type=float, default=1.0, help="Seconds to sleep per step.")
    parser.add_argument(
        "--label",
        default="agent-chronicle-full-demo",
        help="Human-readable label included in the final JSON summary.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.steps < 1:
        raise SystemExit("--steps must be >= 1")
    if args.sleep_seconds < 0:
        raise SystemExit("--sleep-seconds must be >= 0")

    # New names first; pre-rename names still exported by the runner forever.
    run_id = os.environ.get("AGENT_CHRONICLE_RUN_ID") or os.environ.get("AGENT_SENTINEL_RUN_ID", "not-wrapped")
    run_dir = os.environ.get("AGENT_CHRONICLE_RUN_DIR") or os.environ.get("AGENT_SENTINEL_RUN_DIR", "not-wrapped")
    phases = ["inspect", "plan", "edit", "test", "reflect", "summarize"]

    print(f"demo_start label={args.label} run_id={run_id}")
    print(f"demo_run_dir={run_dir}")
    for step in range(1, args.steps + 1):
        phase = phases[(step - 1) % len(phases)]
        print(f"demo_step={step}/{args.steps} phase={phase}")
        if step == max(1, args.steps // 2):
            print("demo_midpoint stderr heartbeat", file=sys.stderr)
        time.sleep(args.sleep_seconds)

    result = {
        "label": args.label,
        "run_id": run_id,
        "steps": args.steps,
        "sleep_seconds": args.sleep_seconds,
        "status": "completed",
        "deliverable": "safe deterministic full-flow demo completed",
    }
    print("demo_result=" + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
