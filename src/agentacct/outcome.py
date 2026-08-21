from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .storage import RunStore


def outcome_path(store: RunStore, run_id: str) -> Path:
    return store.run_dir(run_id) / "outcome.json"


def read_outcome(store: RunStore, run_id: str) -> dict[str, Any] | None:
    path = outcome_path(store, run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_outcome(store: RunStore, run_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
    path = outcome_path(store, run_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(outcome, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return outcome


def _check_status(exit_code: int | None) -> str:
    if exit_code is None:
        return "not_run"
    return "passed" if exit_code == 0 else "failed"


def build_machine_check_outcome(
    *,
    existing: dict[str, Any],
    name: str,
    before_exit_code: int | None,
    after_exit_code: int | None,
    before_summary: str | None = None,
    after_summary: str | None = None,
) -> dict[str, Any]:
    outcome = dict(existing)
    machine_checks = dict(outcome.get("machine_checks") or {})
    checks = list(machine_checks.get("checks") or [])
    before_status = _check_status(before_exit_code)
    after_status = _check_status(after_exit_code)
    resolved_failures = int(before_status == "failed" and after_status == "passed")
    introduced_failures = int(before_status == "passed" and after_status == "failed")
    check = {
        "name": name,
        "recorded_at": time.time(),
        "before": {"exit_code": before_exit_code, "status": before_status, "summary": before_summary},
        "after": {"exit_code": after_exit_code, "status": after_status, "summary": after_summary},
        "resolved_failures": resolved_failures,
        "introduced_failures": introduced_failures,
    }
    checks.append(check)
    machine_checks.update(
        {
            "configured": True,
            "before": before_status,
            "after": after_status,
            "resolved_failures": sum(int(item.get("resolved_failures") or 0) for item in checks),
            "introduced_failures": sum(int(item.get("introduced_failures") or 0) for item in checks),
            "checks": checks,
        }
    )
    outcome["machine_checks"] = machine_checks
    return outcome
