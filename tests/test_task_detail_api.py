"""HTTP and privacy contract for agentacct Task Intelligence."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_chronicle.api import _attach_evidence_to_task_projection, create_local_api_app
from agent_chronicle.service import SentinelService


def _seed_task(store_root: Path) -> tuple[TestClient, str]:
    service = SentinelService(store_root)
    session_id = "private-client-session-id"
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-test",
            "estimated_input_tokens": 120,
            "estimated_output_tokens": 30,
            "estimated_cost_usd": 0.03,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session_id,
                "client_session_kind": "root",
                "client_session_title": "Build a decision-ready product",
                "client_session_title_source": "explicit_client_title_field",
                "client_session_title_sanitized": True,
                "title_redacted": False,
                "cache_creation_tokens_reported": False,
                "cache_read_tokens_reported": True,
                "started_at": 1_750_000_000.0,
                "updated_at": 1_750_000_100.0,
            },
        },
        trusted_usage_import=True,
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "implementation",
                "section_status": "completed",
                "section_title": "Implement the vertical slice",
                "section_summary": "The agent reported completing the implementation.",
                "client": "codex",
                "client_session_id": session_id,
            },
        }
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "codex",
                "client_session_id": session_id,
                "evidence_type": "test",
                "name": "Targeted test suite",
                "result": "passed",
                "exit_code": 0,
                "summary": "All targeted checks passed.",
            },
        }
    )
    return TestClient(create_local_api_app(store_dir=store_root)), session_id


def test_task_detail_uses_opaque_url_and_returns_decision_brief(tmp_path: Path) -> None:
    client, raw_session_id = _seed_task(tmp_path / "state")

    tasks = client.get("/tasks").json()["tasks"]
    public_id = tasks[0]["public_task_id"]
    assert re.fullmatch(r"task_[0-9a-f]{32}", public_id)
    assert raw_session_id not in public_id

    homepage = client.get("/")
    assert homepage.status_code == 200
    assert f'/tasks/{public_id}' in homepage.text
    # Dashboard v2 approved order: the usage pulse and the agent board sit above
    # the recent-activity task feed (hero → tiles → usage → roster → activity).
    assert homepage.text.index('class="usage-pulse"') < homepage.text.index('id="work-feed"')
    assert homepage.text.index("Agents &amp; models") < homepage.text.index('id="work-feed"')

    response = client.get(f"/api/tasks/{public_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == public_id
    assert payload["title"] == "Build a decision-ready product"
    assert set(payload["states"]) == {"execution", "outcome", "control"}
    assert payload["states"]["outcome"]["key"] == "verified"
    assert payload["decision_brief"]["strongest_proof"] == "All targeted checks passed."
    assert payload["decision_brief"]["owner_state"] == "not_recorded"
    assert payload["usage"]["total_tokens"] == 150
    assert payload["timeline"]["total"] == 2
    assert raw_session_id not in response.text

    page = client.get(f"/tasks/{public_id}")
    assert page.status_code == 200
    assert 'name="viewport"' in page.text
    assert 'class="skip-link"' in page.text
    assert "Decision brief" in page.text
    assert "What agentacct can prove" in page.text
    assert "Execution timeline" in page.text
    assert "Agent and model lanes" in page.text
    assert "Estimated from tokens" in page.text
    assert "agentacct does not invent the next action." in page.text
    assert raw_session_id not in page.text


def test_task_detail_unknown_and_malformed_ids_share_generic_404(tmp_path: Path) -> None:
    client, _ = _seed_task(tmp_path / "state")

    malformed = client.get("/api/tasks/view/not-a-task")
    unknown = client.get("/api/tasks/view/task_ffffffffffffffffffffffffffffffff")

    assert malformed.status_code == unknown.status_code == 404
    assert malformed.json() == unknown.json() == {"detail": "Task not found"}


def test_raw_work_reference_collision_never_cross_attaches_evidence() -> None:
    projection = {
        "tasks": [
            {
                "task_id": "first",
                "session_keys": [],
                "work_items": [{"work_id": "shared", "section_id": "shared"}],
            },
            {
                "task_id": "second",
                "session_keys": [],
                "work_items": [{"work_id": "shared", "section_id": "shared"}],
            },
        ]
    }
    evidence = [{"event_id": "evidence-one", "work_id": "shared", "result": "failed"}]

    resolved = _attach_evidence_to_task_projection(projection, evidence)

    assert resolved["tasks"][0]["task_evidence_events"] == []
    assert resolved["tasks"][1]["task_evidence_events"] == []
    assert len(resolved["unassigned_findings"]) == 1
    finding = resolved["unassigned_findings"][0]
    assert finding["event"]["event_id"] == "evidence-one"
    assert finding["assignment_state"] == "ambiguous_task_candidates"
    assert finding["candidate_task_count"] == 2


def _scoped_check(
    *,
    event_id: str,
    result: str,
    session_id: str | None = None,
    source: str = "codex",
    project_identity: str = "project:repo:abc123",
    check_identity: str = "check:security-boundary",
    created_at: float = 100.0,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "source_type": "mcp_agent_reported",
        "source": source,
        "client": source,
        "client_session_id": session_id,
        "project_identity": project_identity,
        "check_identity": check_identity,
        "check_identity_stable": not check_identity.startswith("type:"),
        "evidence_type": "security",
        "result": result,
        "created_at": created_at,
        "summary": f"{event_id}: {result}",
    }


def test_duplicate_event_ids_in_different_sources_never_overwrite_findings() -> None:
    resolved = _attach_evidence_to_task_projection(
        {"tasks": []},
        [
            _scoped_check(event_id="same-id", result="failed", source="codex"),
            _scoped_check(event_id="same-id", result="failed", source="claude-code"),
        ],
    )

    assert resolved["summary"]["total_open_finding_count"] == 2
    assert {finding["event"]["source"] for finding in resolved["unassigned_findings"]} == {
        "codex",
        "claude-code",
    }


def test_same_named_checks_in_different_sessions_do_not_close_each_other() -> None:
    resolved = _attach_evidence_to_task_projection(
        {"tasks": []},
        [
            _scoped_check(event_id="failure", result="failed", session_id="session-a"),
            _scoped_check(
                event_id="pass",
                result="passed",
                session_id="session-b",
                created_at=200.0,
            ),
        ],
    )

    assert resolved["summary"]["unassigned_open_finding_count"] == 1
    assert resolved["unassigned_findings"][0]["event"]["event_id"] == "failure"


def test_unstable_type_only_pass_never_closes_another_failure_episode() -> None:
    resolved = _attach_evidence_to_task_projection(
        {"tasks": []},
        [
            _scoped_check(
                event_id="failure",
                result="failed",
                session_id="same-session",
                check_identity="type:security",
            ),
            _scoped_check(
                event_id="pass",
                result="passed",
                session_id="same-session",
                check_identity="type:security",
                created_at=200.0,
            ),
        ],
    )

    assert resolved["summary"]["unassigned_open_finding_count"] == 1
    assert resolved["unassigned_findings"][0]["event"]["event_id"] == "failure"


def test_assignment_transition_reduces_lifecycle_before_choosing_bucket() -> None:
    task = {
        "task_id": "task-one",
        "session_keys": [{"client": "codex", "client_session_id": "root"}],
        "sessions": [{"client": "codex", "client_session_id": "root"}],
        "work_items": [],
    }
    failure_without_assignment = _scoped_check(event_id="failure", result="failed")
    assigned_retry = _scoped_check(
        event_id="pass",
        result="passed",
        session_id="root",
        created_at=200.0,
    )

    resolved = _attach_evidence_to_task_projection(
        {"tasks": [task]},
        [failure_without_assignment, assigned_retry],
    )

    assert resolved["unassigned_findings"] == []
    assert resolved["tasks"][0]["open_finding_events"] == []
    assert [event["event_id"] for event in resolved["tasks"][0]["current_check_events"]] == ["pass"]


def test_unresolved_work_finding_is_rendered_once_not_duplicated_as_unassigned() -> None:
    failure = _scoped_check(event_id="failure", result="failed")
    failure["work_id"] = "unresolved-work"
    projection = {
        "tasks": [],
        "unresolved_work": [
            {
                "item": {
                    "work_id": "unresolved-work",
                    "section_id": "unresolved-work",
                    "evidence_events": [failure],
                }
            }
        ],
    }

    resolved = _attach_evidence_to_task_projection(projection, [failure])

    assert resolved["unassigned_findings"] == []
    assert resolved["summary"]["unresolved_open_finding_count"] == 1
    assert resolved["summary"]["total_open_finding_count"] == 1
    item = resolved["unresolved_work"][0]["item"]
    assert [event["event_id"] for event in item["open_finding_events"]] == ["failure"]


def test_task_pass_never_closes_same_named_failure_on_unrelated_unresolved_work() -> None:
    failure = _scoped_check(
        event_id="unresolved-failure",
        result="failed",
        session_id="session-b",
    )
    failure["work_id"] = "work-b"
    failure["section_id"] = "work-b"
    task_pass = _scoped_check(
        event_id="task-a-pass",
        result="passed",
        session_id="session-a",
        created_at=200.0,
    )
    projection = {
        "tasks": [
            {
                "task_id": "task-a",
                "session_keys": [{"client": "codex", "client_session_id": "session-a"}],
                "sessions": [{"client": "codex", "client_session_id": "session-a"}],
                "work_items": [],
            }
        ],
        "unresolved_work": [
            {
                "item": {
                    "work_id": "work-b",
                    "section_id": "work-b",
                    "client": "codex",
                    "client_session_id": "session-b",
                    "project_identity": "project:repo:abc123",
                    "evidence_events": [failure],
                }
            }
        ],
    }

    resolved = _attach_evidence_to_task_projection(projection, [failure, task_pass])

    item = resolved["unresolved_work"][0]["item"]
    assert [event["event_id"] for event in item["open_finding_events"]] == [
        "unresolved-failure"
    ]
    assert resolved["summary"]["unresolved_open_finding_count"] == 1
    assert resolved["summary"]["total_open_finding_count"] == 1


def test_raw_work_reference_requires_matching_source_and_full_project_identity() -> None:
    projection = {
        "tasks": [
            {
                "task_id": "task-one",
                "session_keys": [],
                "sessions": [],
                "work_items": [
                    {
                        "work_id": "shared",
                        "section_id": "shared",
                        "client": "codex",
                        "reporting_source": "codex",
                        "project_identity": "project:repo:owner-a",
                    }
                ],
            }
        ]
    }
    foreign = _scoped_check(
        event_id="foreign",
        result="failed",
        source="claude-code",
        project_identity="project:repo:owner-b",
    )
    foreign["work_id"] = "shared"
    foreign["section_id"] = "shared"

    resolved = _attach_evidence_to_task_projection(projection, [foreign])

    assert resolved["tasks"][0]["task_evidence_events"] == []
    assert resolved["tasks"][0]["open_finding_events"] == []
    assert resolved["unassigned_findings"][0]["assignment_state"] == "namespace_mismatch"


@pytest.mark.parametrize(
    ("conflict_key", "event_value"),
    [
        ("client_session_id", "session-b"),
        ("client_transcript_id", "transcript-b"),
    ],
)
def test_raw_work_reference_cannot_override_explicit_session_identity_conflict(
    conflict_key: str,
    event_value: str,
) -> None:
    projection = {
        "tasks": [
            {
                "task_id": "task-one",
                "session_keys": [{"client": "codex", "client_session_id": "session-a"}],
                "sessions": [{"client": "codex", "client_session_id": "session-a"}],
                "work_items": [
                    {
                        "work_id": "shared",
                        "section_id": "shared",
                        "client": "codex",
                        "reporting_source": "codex",
                        "client_session_id": "session-a",
                        "client_transcript_id": "transcript-a",
                        "project_identity": "project:repo:abc123",
                    }
                ],
            }
        ]
    }
    event = _scoped_check(event_id="conflict", result="failed")
    event["work_id"] = "shared"
    event["section_id"] = "shared"
    event[conflict_key] = event_value

    resolved = _attach_evidence_to_task_projection(projection, [event])

    assert resolved["tasks"][0]["open_finding_events"] == []
    assert resolved["unassigned_findings"][0]["assignment_state"] == "namespace_mismatch"


def test_run_hint_cannot_override_explicit_session_identity_conflict() -> None:
    projection = {
        "tasks": [
            {
                "task_id": "task-one",
                "session_keys": [{"client": "codex", "client_session_id": "session-a"}],
                "sessions": [{"client": "codex", "client_session_id": "session-a"}],
                "work_items": [
                    {
                        "work_id": "work-a",
                        "section_id": "work-a",
                        "run_id": "run-r",
                        "client": "codex",
                        "reporting_source": "codex",
                        "client_session_id": "session-a",
                        "project_identity": "project:repo:abc123",
                    }
                ],
            }
        ]
    }
    event = _scoped_check(
        event_id="run-conflict",
        result="failed",
        session_id="session-b",
    )
    event["run_id"] = "run-r"

    resolved = _attach_evidence_to_task_projection(projection, [event])

    assert resolved["tasks"][0]["open_finding_events"] == []
    assert resolved["unassigned_findings"][0]["assignment_state"] == "namespace_mismatch"


def test_raw_ref_cannot_rescue_unknown_session_against_log_evidenced_work() -> None:
    projection = {
        "tasks": [
            {
                "task_id": "task-a",
                "session_keys": [{"client": "codex", "client_session_id": "session-a"}],
                "sessions": [{"client": "codex", "client_session_id": "session-a"}],
                "work_items": [
                    {
                        "work_id": "shared",
                        "section_id": "shared",
                        "client": "codex",
                        "reporting_source": "codex",
                        "project_identity": "project:repo:abc123",
                        "log_evidence_candidate_sessions": [
                            {"client": "codex", "client_session_id": "session-a"}
                        ],
                    }
                ],
            }
        ]
    }
    event = _scoped_check(
        event_id="unknown-session",
        result="failed",
        session_id="session-b",
    )
    event["work_id"] = "shared"
    event["section_id"] = "shared"

    resolved = _attach_evidence_to_task_projection(projection, [event])

    assert resolved["tasks"][0]["open_finding_events"] == []
    assert resolved["unassigned_findings"][0]["event"]["event_id"] == "unknown-session"


def test_task_scoped_retry_never_rescues_explicit_unknown_session_failure() -> None:
    projection = {
        "tasks": [
            {
                "task_id": "task-a",
                "session_keys": [{"client": "codex", "client_session_id": "session-a"}],
                "sessions": [{"client": "codex", "client_session_id": "session-a"}],
                "work_items": [],
            }
        ]
    }
    failure = _scoped_check(
        event_id="foreign-fail",
        result="failed",
        session_id="session-b",
        created_at=200.0,
    )
    task_pass = _scoped_check(
        event_id="task-pass",
        result="passed",
        session_id="session-a",
        created_at=100.0,
    )

    resolved = _attach_evidence_to_task_projection(projection, [task_pass, failure])

    assert resolved["tasks"][0]["open_finding_events"] == []
    assert resolved["unassigned_findings"][0]["event"]["event_id"] == "foreign-fail"


def test_donor_candidate_cannot_override_event_explicit_session() -> None:
    projection = {
        "tasks": [
            {
                "task_id": "task-a",
                "session_keys": [{"client": "codex", "client_session_id": "session-a"}],
                "sessions": [{"client": "codex", "client_session_id": "session-a"}],
                "work_items": [],
            }
        ]
    }
    event = _scoped_check(
        event_id="donor-conflict",
        result="failed",
        session_id="session-b",
    )
    event["log_evidence_candidate_sessions"] = [
        {"client": "codex", "client_session_id": "session-a"}
    ]

    resolved = _attach_evidence_to_task_projection(projection, [event])

    assert resolved["tasks"][0]["open_finding_events"] == []
    assert resolved["unassigned_findings"][0]["event"]["event_id"] == "donor-conflict"


def test_log_evidence_conflict_vetoes_direct_session_candidate() -> None:
    projection = {
        "tasks": [
            {
                "task_id": "task-a",
                "session_keys": [{"client": "codex", "client_session_id": "session-a"}],
                "sessions": [{"client": "codex", "client_session_id": "session-a"}],
                "work_items": [],
            }
        ]
    }
    event = _scoped_check(
        event_id="direct-conflict",
        result="failed",
        session_id="session-a",
    )
    event["log_evidence_conflict"] = {
        "conflicting_keys": ["client_session_id"],
        "claimed": "session-a",
        "evidenced": "session-b",
    }

    resolved = _attach_evidence_to_task_projection(projection, [event])

    assert resolved["tasks"][0]["open_finding_events"] == []
    assert resolved["unassigned_findings"][0]["assignment_state"] == "namespace_mismatch"
    assert resolved["unassigned_findings"][0]["event"]["event_id"] == "direct-conflict"


def test_task_scoped_retry_never_rescues_ambiguous_raw_reference_failure() -> None:
    tasks = []
    for suffix in ("a", "b"):
        tasks.append(
            {
                "task_id": f"task-{suffix}",
                "session_keys": [
                    {"client": "codex", "client_session_id": f"session-{suffix}"}
                ],
                "sessions": [
                    {"client": "codex", "client_session_id": f"session-{suffix}"}
                ],
                "work_items": [
                    {
                        "work_id": "shared",
                        "section_id": "shared",
                        "client": "codex",
                        "reporting_source": "codex",
                        "client_session_id": f"session-{suffix}",
                        "project_identity": "project:repo:abc123",
                    }
                ],
            }
        )
    failure = _scoped_check(event_id="ambiguous", result="failed", created_at=200.0)
    failure["work_id"] = "shared"
    failure["section_id"] = "shared"
    task_pass = _scoped_check(
        event_id="task-a-pass",
        result="passed",
        session_id="session-a",
        created_at=100.0,
    )

    resolved = _attach_evidence_to_task_projection(
        {"tasks": tasks},
        [task_pass, failure],
    )

    assert all(task["open_finding_events"] == [] for task in resolved["tasks"])
    assert resolved["unassigned_findings"][0]["assignment_state"] == "ambiguous_task_candidates"
    assert resolved["unassigned_findings"][0]["event"]["event_id"] == "ambiguous"
