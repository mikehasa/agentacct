from typer.testing import CliRunner

from agentacct.cli import app


def test_agent_loop_demo_uses_checkpoint_language_and_pauses(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "agent-loop-demo",
            "--store-dir",
            str(tmp_path),
            "--run-id",
            "run_cli_checkpoint_demo",
            "--checkpoint-every-steps",
            "2",
            "--on-checkpoint",
            "pause",
        ],
    )

    assert result.exit_code == 0
    assert "checkpoint" in result.output.lower()
    assert "status: checkpoint" in result.output.lower()
    assert (tmp_path / "run_cli_checkpoint_demo" / "agent_loop_summary.json").exists()


def test_agent_loop_demo_report_only_continues(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "agent-loop-demo",
            "--store-dir",
            str(tmp_path),
            "--run-id",
            "run_cli_report_only_demo",
            "--checkpoint-every-steps",
            "2",
            "--on-checkpoint",
            "report",
        ],
    )

    assert result.exit_code == 0
    assert "status: completed" in result.output.lower()
