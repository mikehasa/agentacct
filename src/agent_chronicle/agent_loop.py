from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

LoopStatus = Literal["completed", "checkpoint", "budget_blocked", "failed"]
CheckpointAction = Literal["pause", "report"]


@dataclass
class AgentLoopOptions:
    store_dir: Path | str
    run_id: str
    checkpoint_every_steps: int | None = None
    on_checkpoint: CheckpointAction = "pause"
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None
    max_state_chars: int = 3500


@dataclass
class AgentLoopResult:
    run_id: str
    status: LoopStatus
    reason: str
    steps_attempted: int
    steps_forwarded: int
    checkpoint_due: bool
    summary_path: Path
    final_state: str = ""
    step_results: list[dict[str, Any]] = field(default_factory=list)


RequestStep = Callable[[int, str, str], dict[str, Any]]


def _status_from_response(response: dict[str, Any]) -> tuple[LoopStatus | None, str | None]:
    status_code = int(response.get("status_code") or 0)
    error_type = str(response.get("error_type") or "")
    content = str(response.get("content") or "")
    # frozen wire error type (pre-rename): the proxy emits it forever.
    if status_code == 402 or error_type == "agent_sentinel_budget_exceeded":
        return "budget_blocked", content or "budget exceeded before the next agent step could be forwarded"
    if status_code >= 400:
        return "failed", content or f"step failed with HTTP {status_code}"
    return None, None


def run_agent_like_loop(
    *,
    task: str,
    step_instructions: list[str],
    request_step: RequestStep,
    options: AgentLoopOptions,
) -> AgentLoopResult:
    """Run a tiny external agent loop with checkpoint semantics.

    A checkpoint counts visible outer-loop steps only. It does not inspect or
    constrain a model's internal reasoning/thinking within a single request.
    `on_checkpoint='pause'` stops and writes a report; `on_checkpoint='report'`
    emits a checkpoint event but continues.
    """
    store_dir = Path(options.store_dir).expanduser()
    run_dir = store_dir / options.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state = f"Goal: {task}"
    step_results: list[dict[str, Any]] = []
    steps_forwarded = 0
    checkpoint_due = False
    status: LoopStatus = "completed"
    reason = "completed all planned visible agent steps"

    for index, instruction in enumerate(step_instructions, start=1):
        response = request_step(index, state, instruction)
        step_record = {
            "step": index,
            "instruction": instruction,
            "status_code": response.get("status_code"),
            "forwarded": bool(response.get("forwarded")),
            "actual_provider_cost_usd": response.get("actual_provider_cost_usd"),
            "content_preview": str(response.get("content") or "")[:500],
            "created_at": time.time(),
        }
        step_results.append(step_record)
        if response.get("forwarded"):
            steps_forwarded += 1

        terminal_status, terminal_reason = _status_from_response(response)
        if terminal_status is not None:
            status = terminal_status
            reason = terminal_reason or status
            break

        content = str(response.get("content") or "")
        state = (state + "\n\n" + f"Step {index} result:\n" + content)[-options.max_state_chars :]

        if options.checkpoint_every_steps and index % options.checkpoint_every_steps == 0:
            checkpoint_due = True
            checkpoint_event = {
                "run_id": options.run_id,
                "step": index,
                "action": options.on_checkpoint,
                "reason": f"checkpoint after {index} visible agent steps",
                "created_at": time.time(),
            }
            if options.checkpoint_callback:
                options.checkpoint_callback(checkpoint_event)
            if options.on_checkpoint == "pause":
                status = "checkpoint"
                reason = checkpoint_event["reason"]
                break

    summary_path = run_dir / "agent_loop_summary.json"
    result = AgentLoopResult(
        run_id=options.run_id,
        status=status,
        reason=reason,
        steps_attempted=len(step_results),
        steps_forwarded=steps_forwarded,
        checkpoint_due=checkpoint_due,
        summary_path=summary_path,
        final_state=state,
        step_results=step_results,
    )
    summary = asdict(result)
    summary["summary_path"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
