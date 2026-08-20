import json

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.runner import RunOptions, start_guarded_run


def _make_run(tmp_path):
    dummy = tmp_path / "normal.py"
    dummy.write_text("print('done')\n", encoding="utf-8")
    store_root = tmp_path / "state"
    result = start_guarded_run(["python", str(dummy)], RunOptions(store_dir=store_root, poll_interval=0.05))
    return store_root, result


def test_record_machine_check_updates_report_json_outcome(tmp_path):
    store_root, result = _make_run(tmp_path)

    cli_result = CliRunner().invoke(
        app,
        [
            "outcome",
            "record-machine-check",
            result.run_id,
            "--store-dir",
            str(store_root),
            "--name",
            "pytest",
            "--before-exit-code",
            "1",
            "--after-exit-code",
            "0",
            "--before-summary",
            "1 failing test",
            "--after-summary",
            "tests passed",
        ],
    )

    assert cli_result.exit_code == 0
    assert "resolved=1" in cli_result.output

    report_result = CliRunner().invoke(app, ["report", result.run_id, "--store-dir", str(store_root), "--json"])
    assert report_result.exit_code == 0
    payload = json.loads(report_result.output)
    checks = payload["outcome"]["machine_checks"]
    assert checks["configured"] is True
    assert checks["before"] == "failed"
    assert checks["after"] == "passed"
    assert checks["resolved_failures"] == 1
    assert checks["introduced_failures"] == 0
    assert checks["checks"][0]["name"] == "pytest"


def test_record_machine_check_tracks_introduced_failure(tmp_path):
    store_root, result = _make_run(tmp_path)

    cli_result = CliRunner().invoke(
        app,
        [
            "outcome",
            "record-machine-check",
            result.run_id,
            "--store-dir",
            str(store_root),
            "--name",
            "build",
            "--before-exit-code",
            "0",
            "--after-exit-code",
            "2",
        ],
    )

    assert cli_result.exit_code == 0
    report_result = CliRunner().invoke(app, ["report", result.run_id, "--store-dir", str(store_root), "--json"])
    payload = json.loads(report_result.output)
    checks = payload["outcome"]["machine_checks"]
    assert checks["resolved_failures"] == 0
    assert checks["introduced_failures"] == 1
    assert checks["before"] == "passed"
    assert checks["after"] == "failed"
