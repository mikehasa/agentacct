from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Thread
from typing import Literal

import psutil

from .storage import OWNERSHIP_SCHEMA_VERSION, RunStore


_RUNNER_SHIM = r"""
import os
import signal
import sys

gate = int(sys.argv[1])
token = os.read(gate, 1)
os.close(gate)
if token != b"1":
    raise SystemExit(126)

cancel_requested = False

def on_term(_signum, _frame):
    global cancel_requested
    cancel_requested = True

signal.signal(signal.SIGTERM, on_term)
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
while True:
    try:
        _, status = os.waitpid(child, 0)
        break
    except InterruptedError:
        continue
if cancel_requested:
    while True:
        signal.pause()
if os.WIFEXITED(status):
    raise SystemExit(os.WEXITSTATUS(status))
if os.WIFSIGNALED(status):
    raise SystemExit(128 + os.WTERMSIG(status))
raise SystemExit(1)
"""


class RunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    KILLED = "killed"


@dataclass
class RunOptions:
    store_dir: Path | str | None = None
    max_runtime_seconds: float | None = None
    on_timeout: Literal["pause", "kill"] = "pause"
    repeated_error_threshold: int | None = None
    on_repeated_error: Literal["pause", "kill"] = "pause"
    poll_interval: float = 0.2
    cwd: Path | str | None = None


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    reason: str
    exit_code: int | None
    run_dir: Path
    duration_seconds: float


def _reader(pipe, log_path: Path, *, stderr_counter: Counter[str] | None = None, recent_lines: deque[str] | None = None) -> None:
    with log_path.open("w", encoding="utf-8", buffering=1) as out:
        for line in iter(pipe.readline, ""):
            out.write(line)
            if stderr_counter is not None:
                normalized = line.strip()
                if normalized:
                    stderr_counter[normalized] += 1
                    if recent_lines is not None:
                        recent_lines.append(normalized)
    pipe.close()


def _sample(path: Path, max_chars: int = 1200) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _write_report(run_dir: Path, metadata: dict, result: RunResult) -> None:
    stdout_tail = _sample(run_dir / "stdout.log")
    stderr_tail = _sample(run_dir / "stderr.log")
    report = f"""# agentacct Run Report

- Run ID: `{result.run_id}`
- Status: {result.status.value}
- Reason: {result.reason}
- Exit code: {result.exit_code}
- Duration seconds: {result.duration_seconds:.2f}
- Command: `{' '.join(metadata.get('command', []))}`
- Owned by sentinel: {metadata.get('owned_by_sentinel')}

## stdout tail

```text
{stdout_tail}
```

## stderr tail

```text
{stderr_tail}
```
"""
    (run_dir / "report.md").write_text(report, encoding="utf-8")


def _control_process_group(process_group_id: int, action: Literal["pause", "kill"]) -> RunStatus:
    if action == "pause":
        os.killpg(process_group_id, signal.SIGSTOP)
        return RunStatus.PAUSED
    os.killpg(process_group_id, signal.SIGKILL)
    return RunStatus.KILLED


def _abort_blocked_runner(
    proc: subprocess.Popen[str] | None,
    gate_write: int,
    *,
    pgid: int | None,
    birth_time: float | None,
    executable: str | None,
    cwd: str | None,
    nonce: str,
) -> None:
    if gate_write >= 0:
        try:
            os.close(gate_write)
        except OSError:
            pass
    if proc is None:
        return
    try:
        proc.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    if None in {pgid, birth_time, executable, cwd}:
        return
    try:
        live = psutil.Process(proc.pid)
        exact = (
            abs(float(live.create_time()) - float(birth_time)) <= 0.05
            and os.getpgid(proc.pid) == pgid
            and str(Path(live.exe()).resolve()) == executable
            and str(Path(live.cwd()).resolve()) == cwd
            and live.environ().get("AGENT_CHRONICLE_OWNERSHIP_NONCE") == nonce
        )
    except (OSError, psutil.Error):
        return
    if not exact:
        return
    try:
        os.killpg(int(pgid), signal.SIGKILL)
        proc.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def start_guarded_run(command: list[str], options: RunOptions | None = None) -> RunResult:
    options = options or RunOptions()
    if not command or any(not isinstance(value, str) or not value or "\x00" in value for value in command):
        raise ValueError("command must be a non-empty argv array")
    store = RunStore(options.store_dir)
    run_id = "run_" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    run_dir = store.create_run_dir(run_id)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    stderr_counts: Counter[str] = Counter()
    recent_stderr: deque[str] = deque(maxlen=20)
    start = time.monotonic()
    started_at = time.time()
    ownership_nonce = secrets.token_urlsafe(32)

    child_env = os.environ.copy()
    child_env.update(
        {
            "AGENT_CHRONICLE_RUN_ID": run_id,
            "AGENT_CHRONICLE_RUN_DIR": str(run_dir),
            # Pre-rename names exported additively, forever: existing user
            # scripts read them.
            "AGENT_SENTINEL_RUN_ID": run_id,
            "AGENT_SENTINEL_RUN_DIR": str(run_dir),
            "AGENT_CHRONICLE_OWNERSHIP_NONCE": ownership_nonce,
        }
    )

    gate_read, gate_write = os.pipe()
    proc: subprocess.Popen[str] | None = None
    pgid: int | None = None
    process_birth_time: float | None = None
    process_executable: str | None = None
    process_cwd: str | None = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _RUNNER_SHIM, str(gate_read), *command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            pass_fds=(gate_read,),
            env=child_env,
            cwd=options.cwd,
        )
        os.close(gate_read)
        gate_read = -1
        pgid = os.getpgid(proc.pid)
        live_process = psutil.Process(proc.pid)
        process_birth_time = float(live_process.create_time())
        process_executable = str(Path(live_process.exe()).resolve())
        process_cwd = str(Path(live_process.cwd()).resolve())
        metadata = {
            "run_id": run_id,
            "command": command,
            "pid": proc.pid,
            "process_group_id": pgid,
            # frozen metadata key: historical stores/run metadata carry it forever.
            "owned_by_sentinel": True,
            "cwd": str(options.cwd) if options.cwd is not None else None,
            "started_at_monotonic": start,
            "started_at": started_at,
            "status": "running",
            "ownership_schema_version": OWNERSHIP_SCHEMA_VERSION,
            "process_birth_time": process_birth_time,
            "process_executable": process_executable,
            "process_cwd": process_cwd,
            "ownership_nonce": ownership_nonce,
            # frozen metadata keys: historical stores/logs/files carry the
            # pre-rename env key names forever; the child env additionally gets
            # the AGENT_CHRONICLE_* names (above), but stored metadata keeps this
            # exact shape.
            "env": {
                "AGENT_SENTINEL_RUN_ID": run_id,
                "AGENT_SENTINEL_RUN_DIR": str(run_dir),
            },
        }
        store.write_metadata(run_id, metadata)
        os.write(gate_write, b"1")
    except Exception:
        if gate_read >= 0:
            try:
                os.close(gate_read)
            except OSError:
                pass
        _abort_blocked_runner(
            proc,
            gate_write,
            pgid=pgid,
            birth_time=process_birth_time,
            executable=process_executable,
            cwd=process_cwd,
            nonce=ownership_nonce,
        )
        if proc is not None:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
        try:
            store.update_metadata(
                run_id,
                {"status": "failed", "reason": "launch ownership metadata could not be committed"},
            )
        except (FileNotFoundError, OSError, ValueError):
            pass
        raise
    try:
        os.close(gate_write)
    except OSError:
        pass
    assert proc is not None and proc.stdout is not None and proc.stderr is not None and pgid is not None

    threads = [
        Thread(target=_reader, args=(proc.stdout, stdout_path), daemon=True),
        Thread(target=_reader, args=(proc.stderr, stderr_path), kwargs={"stderr_counter": stderr_counts, "recent_lines": recent_stderr}, daemon=True),
    ]
    for thread in threads:
        thread.start()

    status: RunStatus | None = None
    reason = "process exited"
    exit_code: int | None = None

    while True:
        exit_code = proc.poll()
        if exit_code is not None:
            status = RunStatus.COMPLETED if exit_code == 0 else RunStatus.FAILED
            reason = "process exited"
            break

        elapsed = time.monotonic() - start
        if options.max_runtime_seconds is not None and elapsed >= options.max_runtime_seconds:
            store.verify_owned_process(run_id)
            status = _control_process_group(pgid, options.on_timeout)
            reason = f"timeout after {elapsed:.2f}s"
            exit_code = None
            break

        if options.repeated_error_threshold is not None:
            repeated = [(line, count) for line, count in stderr_counts.items() if count >= options.repeated_error_threshold]
            if repeated:
                line, count = max(repeated, key=lambda item: item[1])
                store.verify_owned_process(run_id)
                status = _control_process_group(pgid, options.on_repeated_error)
                reason = f"repeated stderr line {count} times: {line}"
                exit_code = None
                break

        time.sleep(options.poll_interval)

    duration = time.monotonic() - start
    if status == RunStatus.KILLED:
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    for thread in threads:
        thread.join(timeout=0.2)

    assert status is not None
    metadata.update({"status": status.value, "reason": reason, "exit_code": exit_code, "duration_seconds": duration})
    store.write_metadata(run_id, metadata)
    result = RunResult(run_id=run_id, status=status, reason=reason, exit_code=exit_code, run_dir=run_dir, duration_seconds=duration)
    _write_report(run_dir, metadata, result)
    return result
