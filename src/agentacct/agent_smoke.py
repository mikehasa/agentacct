from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Sequence

from .runner import RunOptions, RunStatus, start_guarded_run


class LiveAgent(StrEnum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


class AgentSmokeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentSmokeSpec:
    agent: LiveAgent
    binary: str
    marker: str
    command: list[str]
    max_runtime_seconds: float


@dataclass(frozen=True)
class AgentSmokeSummary:
    agent: str
    command: list[str]
    run_id: str
    status: str
    exit_code: int | None
    reason: str
    duration_seconds: float
    run_dir: str
    work_dir: str
    expected_marker: str
    marker_found: bool
    metadata_ok: bool
    stdout_log: str
    stderr_log: str
    report_md: str
    metadata_json: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_live_agent_smoke_spec(agent: str | LiveAgent) -> AgentSmokeSpec:
    selected = LiveAgent(agent)
    if selected is LiveAgent.CLAUDE_CODE:
        marker = "AGENT_CHRONICLE_CLAUDE_WRAP_OK"
        command = ["claude", "-p", f"Reply with exactly: {marker}"]
        return AgentSmokeSpec(agent=selected, binary="claude", marker=marker, command=command, max_runtime_seconds=90)
    if selected is LiveAgent.CODEX:
        marker = "AGENT_CHRONICLE_CODEX_WRAP_OK"
        command = [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            f"Reply with exactly: {marker}",
        ]
        return AgentSmokeSpec(agent=selected, binary="codex", marker=marker, command=command, max_runtime_seconds=120)
    raise AgentSmokeError(f"Unsupported live agent: {agent}")


def run_live_agent_smoke(
    agent: str | LiveAgent,
    *,
    store_dir: Path | str | None = None,
    work_dir: Path | str | None = None,
    max_runtime_seconds: float | None = None,
    command_override: Sequence[str] | None = None,
) -> AgentSmokeSummary:
    spec = build_live_agent_smoke_spec(agent)
    command = list(command_override) if command_override is not None else list(spec.command)
    binary = command[0] if command else spec.binary
    if shutil.which(binary) is None:
        raise AgentSmokeError(f"Missing required executable on PATH: {binary}")

    if store_dir is None:
        store_path = Path(tempfile.mkdtemp(prefix=f"agent-chronicle-{spec.agent.value}-smoke-state-"))
    else:
        store_path = Path(store_dir)
        store_path.mkdir(parents=True, exist_ok=True)

    if work_dir is None:
        work_path = Path(tempfile.mkdtemp(prefix=f"agent-chronicle-{spec.agent.value}-smoke-work-"))
    else:
        work_path = Path(work_dir)
        work_path.mkdir(parents=True, exist_ok=True)

    result = start_guarded_run(
        command,
        RunOptions(
            store_dir=store_path,
            max_runtime_seconds=max_runtime_seconds or spec.max_runtime_seconds,
            on_timeout="kill",
            cwd=work_path,
        ),
    )

    metadata_path = result.run_dir / "metadata.json"
    stdout_path = result.run_dir / "stdout.log"
    stderr_path = result.run_dir / "stderr.log"
    report_path = result.run_dir / "report.md"
    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    metadata = _read_json_object(metadata_path)

    marker_found = spec.marker in stdout_text
    metadata_ok = (
        metadata.get("owned_by_sentinel") is True
        and metadata.get("status") == RunStatus.COMPLETED.value
        and metadata.get("exit_code") == 0
        and metadata.get("run_id") == result.run_id
    )

    return AgentSmokeSummary(
        agent=spec.agent.value,
        command=command,
        run_id=result.run_id,
        status=result.status.value,
        exit_code=result.exit_code,
        reason=result.reason,
        duration_seconds=result.duration_seconds,
        run_dir=str(result.run_dir),
        work_dir=str(work_path),
        expected_marker=spec.marker,
        marker_found=marker_found,
        metadata_ok=metadata_ok,
        stdout_log=str(stdout_path),
        stderr_log=str(stderr_path),
        report_md=str(report_path),
        metadata_json=str(metadata_path),
    )


def assert_live_agent_smoke_passed(summary: AgentSmokeSummary) -> None:
    missing = [
        path_name
        for path_name, path in [
            ("metadata_json", summary.metadata_json),
            ("stdout_log", summary.stdout_log),
            ("stderr_log", summary.stderr_log),
            ("report_md", summary.report_md),
        ]
        if not Path(path).exists()
    ]
    if missing:
        raise AgentSmokeError(f"Smoke artifacts missing: {', '.join(missing)}")
    if summary.status != RunStatus.COMPLETED.value or summary.exit_code != 0:
        raise AgentSmokeError(f"Smoke run did not complete successfully: {summary.status} ({summary.reason})")
    if not summary.marker_found:
        raise AgentSmokeError(f"Expected marker not found in stdout.log: {summary.expected_marker}")
    if not summary.metadata_ok:
        raise AgentSmokeError("Smoke metadata did not prove agentacct ownership and successful completion")


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
