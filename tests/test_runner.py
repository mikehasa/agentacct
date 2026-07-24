import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from agent_chronicle.runner import RunOptions, RunStatus, start_guarded_run
import pytest

from agent_chronicle.storage import RunStore


def write_dummy(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


def test_completed_run_writes_metadata_logs_and_report(tmp_path):
    dummy = write_dummy(
        tmp_path / "normal.py",
        "import sys\nprint('hello from dummy')\nprint('warn from dummy', file=sys.stderr)\n",
    )
    store = RunStore(tmp_path / "state")

    result = start_guarded_run(
        [sys.executable, str(dummy)],
        RunOptions(store_dir=store.root, poll_interval=0.05),
    )

    assert result.status == RunStatus.COMPLETED
    assert result.exit_code == 0
    run_dir = store.run_dir(result.run_id)
    assert (run_dir / "stdout.log").read_text() == "hello from dummy\n"
    assert "warn from dummy" in (run_dir / "stderr.log").read_text()
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["command"] == [sys.executable, str(dummy)]
    assert metadata["owned_by_sentinel"] is True
    report = (run_dir / "report.md").read_text()
    assert "Status: completed" in report
    assert "hello from dummy" in report


def test_post_popen_metadata_failure_reaps_blocked_shim_without_running_target(tmp_path, monkeypatch):
    marker = tmp_path / "target-ran"
    dummy = write_dummy(
        tmp_path / "must_not_run.py",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
    )
    captured = {}
    real_popen = subprocess.Popen

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        captured["process"] = process
        return process

    def fail_metadata(_self, _run_id, _metadata):
        raise OSError("simulated metadata fsync failure")

    monkeypatch.setattr("agent_chronicle.runner.subprocess.Popen", capture_popen)
    monkeypatch.setattr(RunStore, "write_metadata", fail_metadata)

    with pytest.raises(OSError, match="fsync"):
        start_guarded_run(
            [sys.executable, str(dummy)],
            RunOptions(store_dir=tmp_path / "state"),
        )

    process = captured["process"]
    assert process.poll() is not None
    assert not marker.exists()


def test_timeout_pauses_only_owned_dummy_process(tmp_path):
    dummy = write_dummy(
        tmp_path / "loop.py",
        "import time\nprint('started', flush=True)\nwhile True:\n    time.sleep(0.2)\n",
    )
    store = RunStore(tmp_path / "state")

    result = start_guarded_run(
        [sys.executable, str(dummy)],
        RunOptions(
            store_dir=store.root,
            max_runtime_seconds=0.35,
            on_timeout="pause",
            poll_interval=0.05,
        ),
    )

    assert result.status == RunStatus.PAUSED
    assert "timeout" in result.reason.lower()
    metadata = json.loads((store.run_dir(result.run_id) / "metadata.json").read_text())
    pid = metadata["pid"]
    proc = psutil.Process(pid)
    try:
        assert proc.status() in {psutil.STATUS_STOPPED, "stopped"}
        verified = store.assert_owned(result.run_id)
        assert verified["process_birth_time"] == proc.create_time()
        assert verified["process_executable"] == str(Path(proc.exe()).resolve())
        assert verified["process_cwd"] == str(Path(proc.cwd()).resolve())
        assert verified["ownership_nonce"]
    finally:
        os.killpg(metadata["process_group_id"], signal.SIGKILL)


def test_repeated_stderr_line_pauses_dummy_process(tmp_path):
    dummy = write_dummy(
        tmp_path / "repeat_error.py",
        "import sys, time\nfor _ in range(20):\n    print('SAME_ERROR dependency exploded', file=sys.stderr, flush=True)\n    time.sleep(0.05)\nwhile True:\n    time.sleep(0.2)\n",
    )
    store = RunStore(tmp_path / "state")

    result = start_guarded_run(
        [sys.executable, str(dummy)],
        RunOptions(
            store_dir=store.root,
            repeated_error_threshold=5,
            on_repeated_error="pause",
            poll_interval=0.03,
        ),
    )

    assert result.status == RunStatus.PAUSED
    assert "repeated stderr" in result.reason.lower()
    assert "SAME_ERROR" in result.reason
    metadata = json.loads((store.run_dir(result.run_id) / "metadata.json").read_text())
    try:
        assert psutil.Process(metadata["pid"]).status() in {psutil.STATUS_STOPPED, "stopped"}
    finally:
        os.killpg(metadata["process_group_id"], signal.SIGKILL)


def test_store_refuses_to_control_unowned_run(tmp_path):
    store = RunStore(tmp_path / "state")
    run_dir = store.root / "runs" / "external_run"
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({"run_id": "external_run", "owned_by_sentinel": False, "pid": os.getpid()}),
        encoding="utf-8",
    )

    try:
        store.assert_owned("external_run")
    except PermissionError as exc:
        assert "not owned by agent-chronicle" in str(exc)
    else:
        raise AssertionError("expected PermissionError for unowned run")


def test_store_refuses_legacy_live_metadata_without_complete_process_proof(tmp_path):
    store = RunStore(tmp_path / "state")
    run_dir = store.create_run_dir("legacy_running")
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run_id": "legacy_running",
                "owned_by_sentinel": True,
                "pid": os.getpid(),
                "process_group_id": os.getpgrp(),
                "status": "running",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PermissionError, match="ownership proof is incomplete"):
        store.assert_owned("legacy_running")


def test_store_rejects_path_traversal_run_ids(tmp_path):
    store = RunStore(tmp_path / "state")

    for malicious_run_id in ["../outside", "run/with/slash", "", ".", "run with spaces"]:
        with pytest.raises(ValueError, match="invalid run_id"):
            store.run_dir(malicious_run_id)


def test_store_accepts_generated_and_legacy_safe_run_ids(tmp_path):
    store = RunStore(tmp_path / "state")

    assert store.run_dir("run_20260618_194139_cca6aeba").parent == store.runs_root
    assert store.run_dir("run_example_test").parent == store.runs_root
