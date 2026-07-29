import json

from agentacct.agent_loop import AgentLoopOptions, run_agent_like_loop


def test_checkpoint_pauses_after_visible_steps_without_calling_next_step(tmp_path):
    calls = []

    def request_step(step_number, state, instruction):
        calls.append((step_number, instruction))
        return {
            "status_code": 200,
            "forwarded": True,
            "content": f"result {step_number}",
            "actual_provider_cost_usd": 0.001,
        }

    result = run_agent_like_loop(
        task="Draft an Agent FinOps MVP",
        step_instructions=["requirements", "architecture", "policies", "cli ux"],
        request_step=request_step,
        options=AgentLoopOptions(
            store_dir=tmp_path,
            run_id="run_checkpoint_test",
            checkpoint_every_steps=3,
            on_checkpoint="pause",
        ),
    )

    assert result.status == "checkpoint"
    assert result.steps_attempted == 3
    assert result.checkpoint_due is True
    assert [call[0] for call in calls] == [1, 2, 3]
    assert "checkpoint" in result.reason.lower()
    summary = json.loads(result.summary_path.read_text())
    assert summary["status"] == "checkpoint"
    assert summary["checkpoint_due"] is True
    assert summary["steps_forwarded"] == 3


def test_checkpoint_can_be_report_only_and_continue_to_budget_or_finish(tmp_path):
    checkpoint_seen = []

    def request_step(step_number, state, instruction):
        return {
            "status_code": 200,
            "forwarded": True,
            "content": f"result {step_number}",
            "actual_provider_cost_usd": 0.0001,
        }

    result = run_agent_like_loop(
        task="Draft an Agent FinOps MVP",
        step_instructions=["requirements", "architecture", "policies"],
        request_step=request_step,
        options=AgentLoopOptions(
            store_dir=tmp_path,
            run_id="run_checkpoint_report_only",
            checkpoint_every_steps=2,
            on_checkpoint="report",
            checkpoint_callback=lambda event: checkpoint_seen.append(event),
        ),
    )

    assert result.status == "completed"
    assert result.steps_attempted == 3
    assert len(checkpoint_seen) == 1
    assert checkpoint_seen[0]["step"] == 2
    assert checkpoint_seen[0]["action"] == "report"


def test_budget_block_from_proxy_stops_loop_as_budget_blocked_not_checkpoint(tmp_path):
    def request_step(step_number, state, instruction):
        if step_number == 2:
            return {
                "status_code": 402,
                "forwarded": False,
                "content": "budget exceeded",
                "actual_provider_cost_usd": None,
                "error_type": "agent_sentinel_budget_exceeded",
            }
        return {
            "status_code": 200,
            "forwarded": True,
            "content": "first result",
            "actual_provider_cost_usd": 0.001,
        }

    result = run_agent_like_loop(
        task="Draft an Agent FinOps MVP",
        step_instructions=["requirements", "architecture", "policies"],
        request_step=request_step,
        options=AgentLoopOptions(store_dir=tmp_path, run_id="run_budget_blocked", checkpoint_every_steps=10),
    )

    assert result.status == "budget_blocked"
    assert result.steps_attempted == 2
    assert result.steps_forwarded == 1
    assert result.checkpoint_due is False
    assert "budget" in result.reason.lower()
