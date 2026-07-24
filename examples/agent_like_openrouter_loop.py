#!/usr/bin/env python3
"""Run a small agent-like task loop through the Agent Chronicle OpenRouter proxy.

This example intentionally talks to the local Chronicle proxy, not directly to
OpenRouter. Start the proxy separately with a hard budget, then run this script.

Example:

  export AGENT_CHRONICLE_OPENROUTER_API_KEY=<OPENROUTER_API_KEY>
  agent-chronicle cost proxy \
    --enable-forwarding \
    --forward-provider openrouter \
    --max-total-usd 0.01

  python examples/agent_like_openrouter_loop.py \
    --model openai/gpt-4o-mini \
    --run-id demo_agent_loop \
    --task "Design a tiny CLI safety layer"
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Iterable

import httpx

DEFAULT_STEPS = [
    "Break down the task into a small implementation plan.",
    "Identify the minimum safe architecture and data flow.",
    "List budget, checkpoint, and failure-mode policies.",
    "Draft CLI user experience and example commands.",
    "Write acceptance criteria for tests and safety checks.",
]


def build_messages(task: str, step_name: str, running_state: str) -> list[dict[str, str]]:
    state = running_state.strip() or "No prior state yet."
    return [
        {
            "role": "system",
            "content": (
                "You are a concise implementation planner. Return a short, "
                "concrete answer. Do not include secrets or credentials."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Task: {task}\n\n"
                f"Current loop step: {step_name}\n\n"
                f"Running state from prior steps:\n{state}\n\n"
                "Produce the next useful artifact for this step in 5 bullets or less."
            ),
        },
    ]


def _extract_text(body: dict[str, Any]) -> str:
    try:
        return str(body["choices"][0]["message"].get("content") or "")
    except Exception:
        return ""


def _extract_actual_cost(envelope: dict[str, Any]) -> float:
    # The envelope rides the frozen "agent_sentinel" wire key (pre-rename).
    value = envelope.get("actual_provider_cost_usd")
    if value is None:
        value = envelope.get("estimated_cost_usd")
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def run_loop(
    *,
    client: Any,
    proxy_base_url: str,
    model: str,
    task: str,
    steps: Iterable[str],
    run_id: str,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    running_state = ""
    events: list[dict[str, Any]] = []
    total_actual_cost = 0.0
    steps_forwarded = 0
    status = "completed"

    endpoint = proxy_base_url.rstrip("/") + "/openrouter/v1/chat/completions"

    for index, step_name in enumerate(steps, start=1):
        payload = {
            "model": model,
            "messages": build_messages(task, step_name, running_state),
            "max_tokens": max_tokens,
        }
        try:
            response = client.post(
                endpoint,
                headers={"X-Agent-Sentinel-Run-Id": run_id},
                json=payload,
                timeout=timeout,
            )
        except Exception as exc:
            status = "error"
            events.append(
                {
                    "step": index,
                    "name": step_name,
                    "status_code": None,
                    "decision": "proxy_error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "actual_or_estimated_cost_usd": 0.0,
                }
            )
            break
        try:
            body = response.json()
        except Exception:
            body = {"error": {"message": "non-json response"}}

        envelope = body.get("agent_sentinel") or {}
        event = {
            "step": index,
            "name": step_name,
            "status_code": response.status_code,
            "decision": envelope.get("decision"),
            "usage_confidence": envelope.get("usage_confidence"),
            "cost_confidence": envelope.get("cost_confidence"),
            "actual_or_estimated_cost_usd": _extract_actual_cost(envelope),
        }
        events.append(event)

        if response.status_code >= 400:
            status = "blocked"
            event["error_type"] = (body.get("error") or {}).get("type")
            event["error_message"] = (body.get("error") or {}).get("message")
            break

        steps_forwarded += 1
        total_actual_cost += event["actual_or_estimated_cost_usd"]
        text = _extract_text(body).strip()
        running_state = (running_state + f"\n\nStep {index} ({step_name}):\n{text}").strip()

    return {
        "run_id": run_id,
        "model": model,
        "task": task,
        "status": status,
        "steps_attempted": len(events),
        "steps_forwarded": steps_forwarded,
        "actual_provider_cost_usd": round(total_actual_cost, 10),
        "events": events,
        "final_state_preview": running_state[-2000:],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--model", default="openai/gpt-4o-mini")
    parser.add_argument("--task", default="Design a tiny agent safety feature")
    parser.add_argument("--run-id", default=f"example_agent_loop_{int(time.time())}")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--step", dest="steps", action="append", help="Custom step. Repeat to override defaults.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_loop(
        client=httpx.Client(),
        proxy_base_url=args.proxy_base_url,
        model=args.model,
        task=args.task,
        steps=args.steps or DEFAULT_STEPS,
        run_id=args.run_id,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
