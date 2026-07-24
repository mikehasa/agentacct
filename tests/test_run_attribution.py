import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from agent_chronicle.cli import app
from agent_chronicle.cost import CostLedger, estimate_openai_chat_usage
from agent_chronicle.runner import RunOptions, start_guarded_run


def test_guarded_run_injects_run_identity_env_vars(tmp_path):
    dummy = tmp_path / "print_env.py"
    dummy.write_text(
        "import os\n"
        "print(os.environ.get('AGENT_CHRONICLE_RUN_ID', 'missing'))\n"
        "print(os.environ.get('AGENT_CHRONICLE_RUN_DIR', 'missing'))\n"
        # pre-rename names stay exported into child env forever
        "print(os.environ.get('AGENT_SENTINEL_RUN_ID', 'missing'))\n"
        "print(os.environ.get('AGENT_SENTINEL_RUN_DIR', 'missing'))\n",
        encoding="utf-8",
    )

    result = start_guarded_run([sys.executable, str(dummy)], RunOptions(store_dir=tmp_path / "state", poll_interval=0.05))

    stdout_lines = (result.run_dir / "stdout.log").read_text(encoding="utf-8").splitlines()
    assert stdout_lines[0] == result.run_id
    assert stdout_lines[1] == str(result.run_dir)
    assert stdout_lines[2] == result.run_id
    assert stdout_lines[3] == str(result.run_dir)
    metadata = json.loads((result.run_dir / "metadata.json").read_text(encoding="utf-8"))
    # stored run metadata keeps the FROZEN pre-rename key names forever
    assert metadata["env"]["AGENT_SENTINEL_RUN_ID"] == result.run_id
    assert "AGENT_CHRONICLE_RUN_ID" not in metadata["env"]


def test_cost_ledger_filters_and_totals_by_run_id(tmp_path):
    ledger = CostLedger(tmp_path)
    usage_a = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello A"}]})
    usage_b = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello B with more words"}]})
    ledger.record_usage(usage_a, run_id="run_a", decision="dry_run_allow")
    ledger.record_usage(usage_b, run_id="run_b", decision="dry_run_allow")
    ledger.record_usage(usage_a, run_id="run_a", decision="dry_run_allow")

    run_a_events = ledger.read_events(run_id="run_a")

    assert len(run_a_events) == 2
    assert all(event["run_id"] == "run_a" for event in run_a_events)
    assert ledger.total_estimated_cost_usd(run_id="run_a") == sum(event["estimated_cost_usd"] for event in run_a_events)
    assert ledger.total_estimated_cost_usd() > ledger.total_estimated_cost_usd(run_id="run_a")


def test_proxy_header_run_id_is_visible_in_cost_status(tmp_path):
    from fastapi.testclient import TestClient
    from agent_chronicle.cost import CostPolicy
    from agent_chronicle.proxy import create_app

    client = TestClient(create_app(store_dir=tmp_path, policy=CostPolicy(max_total_usd=1.0), dry_run=True))
    response = client.post(
        "/openai/v1/chat/completions",
        headers={"X-Agent-Sentinel-Run-Id": "run_header_demo"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200

    result = CliRunner().invoke(app, ["cost", "status", "--store-dir", str(tmp_path), "--run-id", "run_header_demo"])

    assert result.exit_code == 0
    assert "Run ID: run_header_demo" in result.output
    assert "Events: 1" in result.output
