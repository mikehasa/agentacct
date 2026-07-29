from __future__ import annotations

from pathlib import Path
from typing import Any

from .cost import CostLedger
from .outcome import read_outcome
from .storage import RunStore


def _count_by(events: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        value = str(event.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _sum_cost_by(events: list[dict[str, Any]], key: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for event in events:
        value = str(event.get(key) or "unknown")
        cost = float(event.get("estimated_cost_usd") or 0.0)
        totals[value] = totals.get(value, 0.0) + cost
    return dict(sorted(totals.items()))


def _tail(path: Path, max_chars: int = 1200) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def default_outcome_schema(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return the v0 outcome schema used by CLI, sidecar, and future MCP tools.

    The first version deliberately separates objective machine facts from
    subjective value judgment. Most fields start as not_configured/not_evaluated
    until later stages add git/test/judge collectors.
    """
    return {
        "status": metadata.get("status") or "unknown",
        "exit_code": metadata.get("exit_code"),
        "duration_seconds": metadata.get("duration_seconds"),
        "machine_checks": {
            "configured": False,
            "before": None,
            "after": None,
            "resolved_failures": None,
            "introduced_failures": None,
        },
        "git": {
            "tracked": False,
            "files_changed": None,
            "lines_added": None,
            "lines_deleted": None,
        },
        "human_review": {
            "status": "not_requested",
            "notes": None,
        },
        "judge": {
            "enabled": False,
            "source": "none",
            "deliverable_score": None,
            "confidence": "not_evaluated",
            "reason": None,
        },
        "value": {
            "score": None,
            "rating": "not_evaluated",
            "confidence": "not_evaluated",
            "reason": "Value scoring requires outcome evidence and is not evaluated in the v0 schema.",
        },
    }


def build_run_report_payload(store: RunStore, run_id: str) -> dict[str, Any]:
    metadata = store.read_metadata(run_id)
    ledger = CostLedger(store.root)
    events = ledger.read_events(run_id=run_id)
    run_dir = store.run_dir(run_id)
    cost_confidence = _count_by(events, "cost_confidence")
    usage_confidence = _count_by(events, "usage_confidence")
    outcome = read_outcome(store, run_id) or default_outcome_schema(metadata)
    return {
        # frozen: historical report files carry this schema string forever.
        "schema_version": "agent-sentinel.report.v1",
        "run": {
            "run_id": run_id,
            "status": metadata.get("status") or "unknown",
            "reason": metadata.get("reason"),
            "exit_code": metadata.get("exit_code"),
            "duration_seconds": metadata.get("duration_seconds"),
            "command": metadata.get("command") or [],
            "owned_by_sentinel": bool(metadata.get("owned_by_sentinel")),
        },
        "cost": {
            "event_count": len(events),
            "estimated_total_cost_usd": ledger.total_estimated_cost_usd(run_id=run_id),
            "actual_provider_cost_usd": ledger.total_actual_provider_cost_usd(run_id=run_id),
            "billable_cost_usd": ledger.total_billable_cost_usd(run_id=run_id),
            "estimated_total_tokens": ledger.total_estimated_tokens(run_id=run_id),
            "estimated_input_tokens": ledger.total_estimated_input_tokens(run_id=run_id),
            "estimated_output_tokens": ledger.total_estimated_output_tokens(run_id=run_id),
            "by_provider_estimated_usd": _sum_cost_by(events, "provider"),
            "by_model_estimated_usd": _sum_cost_by(events, "model"),
            "usage_confidence_counts": usage_confidence,
            "cost_confidence_counts": cost_confidence,
        },
        "interruptions": {
            "cutoff_triggered": any(event.get("decision") == "blocked" for event in events),
            "blocked_events": sum(1 for event in events if event.get("decision") == "blocked"),
            "checkpoints": 0,
        },
        "outcome": outcome,
        "artifacts": {
            "run_dir": str(run_dir),
            "report_md": str(run_dir / "report.md"),
            "stdout_log": str(run_dir / "stdout.log"),
            "stderr_log": str(run_dir / "stderr.log"),
            "stdout_tail": _tail(run_dir / "stdout.log"),
            "stderr_tail": _tail(run_dir / "stderr.log"),
            "contains_log_tails": True,
            "share_safety": "local/private report: stdout_tail and stderr_tail may contain sensitive user, tool, or process output; review before sharing externally",
        },
    }
