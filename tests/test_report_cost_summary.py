import json

from typer.testing import CliRunner

from agent_chronicle.cli import app
from agent_chronicle.cost import CostLedger, estimate_openai_chat_usage, estimate_anthropic_messages_usage
from agent_chronicle.runner import RunOptions, start_guarded_run


def test_report_command_includes_cost_summary_for_run(tmp_path):
    dummy = tmp_path / "normal.py"
    dummy.write_text("print('done')\n", encoding="utf-8")
    store_root = tmp_path / "state"
    result = start_guarded_run(["python", str(dummy)], RunOptions(store_dir=store_root, poll_interval=0.05))

    ledger = CostLedger(store_root)
    ledger.record_usage(
        estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]}),
        run_id=result.run_id,
        decision="dry_run_allow",
    )
    ledger.record_usage(
        estimate_anthropic_messages_usage({"model": "claude-sonnet-4", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]}),
        run_id=result.run_id,
        decision="dry_run_allow",
    )

    cli_result = CliRunner().invoke(app, ["report", result.run_id, "--store-dir", str(store_root)])

    assert cli_result.exit_code == 0
    assert "## Cost Summary" in cli_result.output
    assert "Cost events: 2" in cli_result.output
    assert "openai" in cli_result.output
    assert "anthropic" in cli_result.output
    assert "Estimated total cost:" in cli_result.output


def test_report_command_shows_no_cost_events_when_none_exist(tmp_path):
    dummy = tmp_path / "normal.py"
    dummy.write_text("print('done')\n", encoding="utf-8")
    store_root = tmp_path / "state"
    result = start_guarded_run(["python", str(dummy)], RunOptions(store_dir=store_root, poll_interval=0.05))

    cli_result = CliRunner().invoke(app, ["report", result.run_id, "--store-dir", str(store_root)])

    assert cli_result.exit_code == 0
    assert "## Cost Summary" in cli_result.output
    assert "Cost events: 0" in cli_result.output


def test_report_json_includes_cost_runtime_and_outcome_schema(tmp_path):
    dummy = tmp_path / "normal.py"
    dummy.write_text("print('done')\n", encoding="utf-8")
    store_root = tmp_path / "state"
    result = start_guarded_run(["python", str(dummy)], RunOptions(store_dir=store_root, poll_interval=0.05))

    ledger = CostLedger(store_root)
    ledger.record_usage(
        estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]}),
        run_id=result.run_id,
        decision="dry_run_allow",
    )

    cli_result = CliRunner().invoke(app, ["report", result.run_id, "--store-dir", str(store_root), "--json"])

    assert cli_result.exit_code == 0
    payload = json.loads(cli_result.output)
    assert payload["schema_version"] == "agent-sentinel.report.v1"
    assert payload["run"]["run_id"] == result.run_id
    assert payload["run"]["status"] == "completed"
    assert payload["cost"]["event_count"] == 1
    assert payload["cost"]["estimated_total_cost_usd"] > 0
    assert payload["cost"]["by_provider_estimated_usd"]["openai"] > 0
    assert payload["outcome"]["status"] == "completed"
    assert payload["outcome"]["machine_checks"]["configured"] is False
    assert payload["outcome"]["judge"]["deliverable_score"] is None
    assert payload["outcome"]["value"]["rating"] == "not_evaluated"
    assert "stdout_tail" in payload["artifacts"]
    assert payload["artifacts"]["contains_log_tails"] is True
    assert "local/private" in payload["artifacts"]["share_safety"]


def test_report_json_marks_blocked_cost_events_as_interruptions(tmp_path):
    dummy = tmp_path / "normal.py"
    dummy.write_text("print('done')\n", encoding="utf-8")
    store_root = tmp_path / "state"
    result = start_guarded_run(["python", str(dummy)], RunOptions(store_dir=store_root, poll_interval=0.05))

    ledger = CostLedger(store_root)
    ledger.record_usage(
        estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]}),
        run_id=result.run_id,
        decision="blocked",
        reason="budget would be exceeded",
    )

    cli_result = CliRunner().invoke(app, ["report", result.run_id, "--store-dir", str(store_root), "--json"])

    assert cli_result.exit_code == 0
    payload = json.loads(cli_result.output)
    assert payload["interruptions"]["cutoff_triggered"] is True
    assert payload["interruptions"]["blocked_events"] == 1
    assert payload["cost"]["billable_cost_usd"] == 0.0
