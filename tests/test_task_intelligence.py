from __future__ import annotations

from agentacct.task_intelligence import build_task_intelligence


def _task() -> dict[str, object]:
    return {
        "primary_root": {"client": "codex", "client_session_id": "root"},
        "root_keys": [{"client": "codex", "client_session_id": "root"}],
        "sessions": [
            {
                "client": "codex",
                "client_session_id": "root",
                "session_kind": "root",
                "first_activity_at": 1.0,
                "last_activity_at": 10.0,
                "usage": {"rows": 1, "total_tokens": 120, "model_lanes": [{"model": "gpt-5"}]},
            },
            {
                "client": "codex",
                "client_session_id": "child",
                "session_kind": "internal",
                "first_activity_at": 2.0,
                "last_activity_at": 12.0,
                "usage": {"rows": 1, "total_tokens": 30, "model_lanes": [{"model": "gpt-5-mini"}]},
            },
        ],
        "usage": {"rows": 2, "total_tokens": 150, "estimated_cost_usd": None},
        "models": ["gpt-5", "gpt-5-mini"],
        "work_items": [
            {
                "work_id": "work-1",
                "title": "Implement feature",
                "latest_status": "completed",
                "client": "codex",
                "client_session_id": "root",
                "updated_at": 10.0,
                "next_step": "Ship it",
                "evidence_events": [
                    {"event_id": "check-1", "result": "failed", "summary": "Target test failed", "created_at": 11.0}
                ],
            }
        ],
    }


def test_decision_brief_keeps_finding_separate_from_control_failure() -> None:
    result = build_task_intelligence(_task(), public_task_id="task_" + "1" * 32, title="Feature")

    assert result["states"]["execution"]["key"] == "finished"
    assert result["states"]["outcome"]["key"] == "finding"
    assert result["states"]["control"]["key"] == "ready"
    assert result["decision_brief"]["unresolved_finding"] == "Target test failed"
    assert result["decision_brief"]["owner"] is None
    assert result["decision_brief"]["next_action"] is None


def test_task_usage_is_not_readded_from_work_or_lanes() -> None:
    result = build_task_intelligence(_task(), public_task_id="task_" + "2" * 32, title="Feature")

    assert result["usage"]["total_tokens"] == 150
    assert result["usage"]["rows"] == 2
    assert result["duration_seconds"] == 11.0
    assert result["coverage"][3]["state"] == "partial"
    assert result["lanes"][0]["models"] == ["gpt-5"]
    assert result["lanes"][1]["role"] == "supporting"
    assert result["lanes"][1]["session_count"] == 1


def test_internal_and_child_sessions_collapse_into_one_supporting_lane_per_client() -> None:
    task = _task()
    task["sessions"] = list(task["sessions"]) + [  # type: ignore[arg-type]
        {
            "client": "codex",
            "client_session_id": "child-2",
            "session_kind": "child",
            "usage": {"rows": 1, "total_tokens": 20, "model_lanes": [{"model": "gpt-5"}]},
        },
        {
            "client": "codex",
            "client_session_id": "internal-2",
            "session_kind": "internal",
            "usage": {"rows": 1, "total_tokens": 10, "model_lanes": [{"model": "gpt-5-mini"}]},
        },
    ]

    result = build_task_intelligence(
        task, public_task_id="task_" + "7" * 32, title="Collapsed lanes"
    )

    assert len(result["lanes"]) == 2
    supporting = result["lanes"][1]
    assert supporting["role"] == "supporting"
    assert supporting["role_label"] == "3 supporting sessions"
    assert supporting["session_count"] == 3
    assert supporting["session_kinds"] == ["internal", "child"]
    assert supporting["models"] == ["gpt-5-mini", "gpt-5"]
    assert supporting["usage"] == {"rows": 3, "total_tokens": 60}


def test_control_attempt_populates_independent_execution_and_control_axes() -> None:
    control = {
        "attempts": [
            {"attempt_id": "attempt-1", "execution_state": "running", "control_state": "policy_hold", "started_at": 12.0}
        ],
        "events": [],
    }

    result = build_task_intelligence(
        _task(), public_task_id="task_" + "3" * 32, title="Feature", control=control
    )

    assert result["states"]["execution"]["key"] == "running"
    assert result["states"]["control"]["key"] == "policy_hold"
    assert any(event["kind"] == "attempt" for event in result["timeline"]["events"])


def test_large_timeline_is_bounded_but_retains_check() -> None:
    task = _task()
    task["work_items"] = [
        {"work_id": f"w-{index}", "title": f"Step {index}", "latest_status": "completed", "updated_at": index}
        for index in range(80)
    ] + list(task["work_items"])

    result = build_task_intelligence(
        task, public_task_id="task_" + "4" * 32, title="Large", timeline_limit=20
    )

    assert result["timeline"]["truncated"] is True
    assert result["timeline"]["total"] == 82
    assert len(result["timeline"]["events"]) <= 25
    assert any(event["kind"] == "check" for event in result["timeline"]["events"])


def test_later_pass_for_same_check_identity_clears_historical_failure() -> None:
    task = _task()
    task["work_items"][0]["evidence_events"] = [  # type: ignore[index]
        {
            "event_id": "failed-first",
            "check_identity": "target-tests",
            "name": "Target tests",
            "result": "failed",
            "summary": "Old failure",
            "created_at": 11.0,
        },
        {
            "event_id": "passed-rerun",
            "check_identity": "target-tests",
            "name": "Target tests",
            "result": "passed",
            "summary": "Rerun passed",
            "created_at": 12.0,
        },
    ]

    result = build_task_intelligence(
        task, public_task_id="task_" + "5" * 32, title="Resolved"
    )

    assert result["states"]["outcome"]["key"] == "verified"
    assert result["decision_brief"]["strongest_proof"] == "Rerun passed"
    assert result["decision_brief"]["unresolved_finding"] is None
    assert [event["status"] for event in result["timeline"]["events"] if event["kind"] == "check"] == [
        "failed",
        "passed",
    ]


def test_new_pending_retry_wins_over_older_started_attempt_for_current_state() -> None:
    control = {
        "attempts": [
            {
                "attempt_id": "attempt_old",
                "created_at": 10.0,
                "started_at": 20.0,
                "ended_at": 30.0,
                "execution_state": "succeeded",
                "outcome_state": "verified",
                "control_state": "ready",
            },
            {
                "attempt_id": "attempt_retry",
                "created_at": 40.0,
                "started_at": None,
                "ended_at": None,
                "execution_state": "pending",
                "outcome_state": "unknown",
                "control_state": "awaiting_approval",
            },
        ],
        "approvals": [],
        "events": [],
    }

    result = build_task_intelligence(
        _task(),
        public_task_id="task_" + "6" * 32,
        title="Retry state",
        control=control,
    )

    assert result["states"]["execution"]["key"] == "queued"
    assert result["states"]["control"]["key"] == "awaiting_approval"
