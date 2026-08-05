"""Product semantics for agent-discovered findings in the /tasks projection."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agentacct.api as api_module
from agentacct.api import create_local_api_app
from agentacct.service import SentinelService


def _record_root_usage(service: SentinelService, *, session_id: str) -> None:
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-test",
            "estimated_input_tokens": 20,
            "estimated_output_tokens": 5,
            "usage_confidence": "client_reported",
            "cost_confidence": "unknown",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session_id,
                "client_session_kind": "root",
                "client_session_title": "Inspect prediction engine",
                "client_session_title_source": "explicit_client_title_field",
                "client_session_title_sanitized": True,
                "title_redacted": False,
                "cache_creation_tokens_reported": False,
                "cache_read_tokens_reported": True,
                "started_at": 1_750_000_000.0,
                "updated_at": 1_750_000_000.0,
            },
        },
        trusted_usage_import=True,
    )


def test_agent_check_failure_is_an_open_finding_not_a_chronicle_failure(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session_id = "prediction-root"
    _record_root_usage(service, session_id=session_id)
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "review-engine",
                "section_status": "completed",
                "section_title": "Review prediction engine",
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
                "evidence_type": "security",
                "name": "Share math boundary probe",
                "result": "failed",
                "exit_code": 0,
                "summary": "A negative buy produces an impossible share balance.",
            },
        }
    )

    client = TestClient(create_local_api_app(store_dir=store_root))
    projection = client.get("/tasks").json()

    # The failure is an open finding on the task it belongs to...
    assert projection["summary"]["assigned_open_finding_count"] == 1
    assert projection["summary"]["unassigned_open_finding_count"] == 0
    assert projection["summary"]["total_open_finding_count"] == 1
    tasks_with_findings = [
        task for task in projection["tasks"] if task.get("open_finding_events")
    ]
    assert len(tasks_with_findings) == 1
    finding_event = tasks_with_findings[0]["open_finding_events"][0]
    assert finding_event["summary"] == "A negative buy produces an impossible share balance."
    episodes = tasks_with_findings[0]["finding_episodes"]
    assert [episode["attention_open"] for episode in episodes] == [True]
    # ...with no follow-up action recorded yet...
    assert episodes[0]["disposition_state"] == "open"
    assert episodes[0]["assignment"] == "assigned"
    # ...and the failure is about the work being reviewed: it never surfaces
    # as an agentacct-health attention item of its own.
    attention_response = client.get("/attention")
    assert attention_response.status_code == 200
    assert "Share math boundary probe" not in attention_response.text
    assert "A negative buy produces an impossible share balance." not in attention_response.text


def test_unassigned_failure_stays_visible_without_becoming_needs_input(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "project_dir": str(tmp_path / "prediction-market"),
                "evidence_type": "security",
                "name": "Share math boundary probe",
                "result": "failed",
                "summary": "A negative buy produces an impossible share balance.",
            },
        }
    )

    client = TestClient(create_local_api_app(store_dir=store_root))
    projection = client.get("/tasks").json()

    assert projection["summary"]["unassigned_open_finding_count"] == 1
    assert projection["summary"]["total_open_finding_count"] == 1
    assert len(projection["unassigned_findings"]) == 1
    unassigned = projection["unassigned_findings"][0]
    assert unassigned["assignment_state"] == "no_task_candidate"
    assert (
        unassigned["event"]["summary"]
        == "A negative buy produces an impossible share balance."
    )
    assert (
        unassigned["reason"]
        == "This check has no deterministic Task context. agentacct kept the finding visible without inventing one."
    )
    # Staying visible must not turn it into an agentacct needs-input item.
    assert client.get("/attention").json()["total_items"] == 0


def test_unassigned_same_check_pass_closes_failure_but_unrelated_pass_does_not(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    project_dir = str(tmp_path / "prediction-market")

    def record(*, name: str, result: str) -> None:
        service.record_event(
            {
                "source": "codex",
                "event_type": "machine_check",
                "metadata": {
                    "sentinel_semantic_kind": "evidence",
                    "project_dir": project_dir,
                    "evidence_type": "security",
                    "name": name,
                    "result": result,
                    "summary": f"{name}: {result}",
                },
            }
        )

    record(name="Share math boundary probe", result="failed")
    record(name="Unrelated lint", result="passed")
    client = TestClient(create_local_api_app(store_dir=store_root))
    assert client.get("/tasks").json()["summary"]["unassigned_open_finding_count"] == 1

    record(name="Share math boundary probe", result="passed")
    projection = client.get("/tasks").json()

    assert projection["summary"]["unassigned_open_finding_count"] == 0
    assert projection["summary"]["total_open_finding_count"] == 0
    assert projection["unassigned_findings"] == []


def test_open_finding_metric_counts_episodes_across_task_and_unassigned_buckets(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session_id = "prediction-root"
    _record_root_usage(service, session_id=session_id)
    for name in ("Boundary probe", "Reserve invariant"):
        service.record_event(
            {
                "source": "codex",
                "event_type": "machine_check",
                "metadata": {
                    "sentinel_semantic_kind": "evidence",
                    "client": "codex",
                    "client_session_id": session_id,
                    "evidence_type": "security",
                    "name": name,
                    "result": "failed",
                    "summary": f"{name} failed.",
                },
            }
        )
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "evidence_type": "security",
                "name": "Unassigned controller probe",
                "result": "failed",
                "summary": "The controller probe failed without deterministic Task context.",
            },
        }
    )

    client = TestClient(create_local_api_app(store_dir=store_root))
    projection = client.get("/tasks").json()

    assert projection["summary"]["assigned_open_finding_count"] == 2
    assert projection["summary"]["unassigned_open_finding_count"] == 1
    assert projection["summary"]["total_open_finding_count"] == 3


def test_project_store_filters_same_basename_foreign_findings_by_full_identity(
    tmp_path: Path,
) -> None:
    own_project = tmp_path / "owner-a" / "repo"
    foreign_project = tmp_path / "owner-b" / "repo"
    store_root = own_project / ".agent-sentinel" / "state"
    service = SentinelService(store_root)
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "project_dir": str(foreign_project),
                "evidence_type": "security",
                "name": "Foreign same-basename finding",
                "result": "failed",
                "summary": "This belongs to the other repo directory.",
            },
        }
    )
    # Missing full identity is retained for legacy project-store rows; an
    # explicit mismatched identity is never allowed to borrow the basename.
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "evidence_type": "security",
                "name": "Legacy local finding",
                "result": "failed",
                "summary": "A legacy project-store row has no project path.",
            },
        }
    )

    client = TestClient(create_local_api_app(store_dir=store_root))
    response = client.get("/tasks")
    projection = response.json()

    assert projection["summary"]["unassigned_open_finding_count"] == 1
    assert projection["summary"]["total_open_finding_count"] == 1
    assert [
        finding["event"]["summary"] for finding in projection["unassigned_findings"]
    ] == ["A legacy project-store row has no project path."]
    assert "This belongs to the other repo directory." not in response.text


def test_project_store_quarantines_pathless_check_from_filtered_foreign_session(
    tmp_path: Path,
) -> None:
    own_project = tmp_path / "owner-a" / "repo"
    foreign_project = tmp_path / "owner-b" / "repo"
    store_root = own_project / ".agent-sentinel" / "state"
    service = SentinelService(store_root)
    session_id = "foreign-session"
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-test",
            "estimated_input_tokens": 20,
            "estimated_output_tokens": 5,
            "usage_confidence": "client_reported",
            "cost_confidence": "unknown",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session_id,
                "client_session_kind": "root",
                "project_dir": str(foreign_project),
                "started_at": 1_750_000_000.0,
                "updated_at": 1_750_000_000.0,
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
                "section_id": "foreign-work",
                "section_status": "completed",
                "section_title": "Foreign assigned work",
                "client": "codex",
                "client_session_id": session_id,
                "project_dir": str(foreign_project),
            },
        }
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "section_id": "foreign-work",
                "client": "codex",
                "client_session_id": session_id,
                "evidence_type": "security",
                "name": "Foreign assigned boundary probe",
                "result": "failed",
                "summary": "FOREIGN ASSIGNED FINDING",
            },
        }
    )

    client = TestClient(create_local_api_app(store_dir=store_root))
    response = client.get("/tasks")
    projection = response.json()

    assert projection["tasks"] == []
    assert projection["unassigned_findings"] == []
    assert projection["summary"]["total_open_finding_count"] == 0
    assert "FOREIGN ASSIGNED FINDING" not in response.text


def test_project_store_quarantines_session_with_conflicting_full_project_identities(
    tmp_path: Path,
) -> None:
    own_project = tmp_path / "owner-a" / "repo"
    foreign_project = tmp_path / "owner-b" / "repo"
    store_root = own_project / ".agent-sentinel" / "state"
    service = SentinelService(store_root)
    session_id = "colliding-session"
    for index, project in enumerate((own_project, foreign_project), start=1):
        service.record_event(
            {
                "source": "codex-local-session-import",
                "event_type": "model_usage",
                "provider": "codex",
                "model": f"gpt-test-{index}",
                "estimated_input_tokens": 20,
                "estimated_output_tokens": 5,
                "usage_confidence": "client_reported",
                "cost_confidence": "unknown",
                "metadata": {
                    "usage_source": "local_client_session_store",
                    "client": "codex",
                    "client_session_id": session_id,
                    "client_session_kind": "root",
                    "project_dir": str(project),
                    "started_at": 1_750_000_000.0 + index,
                    "updated_at": 1_750_000_000.0 + index,
                },
            },
            trusted_usage_import=True,
        )
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "codex",
                "client_session_id": session_id,
                "evidence_type": "security",
                "name": "Conflicting session pathless probe",
                "result": "failed",
                "summary": "CONFLICTING SESSION PATHLESS FINDING",
            },
        }
    )

    client = TestClient(create_local_api_app(store_dir=store_root))
    projection = client.get("/tasks").json()

    assert projection["tasks"] == []
    assert projection["unassigned_findings"] == []
    assert projection["summary"]["task_count"] == 0
    assert projection["summary"]["total_open_finding_count"] == 0


def test_project_store_identity_does_not_resolve_away_matching_path_alias(
    tmp_path: Path,
) -> None:
    real_owner = tmp_path / "real-owner"
    real_owner.mkdir()
    alias_owner = tmp_path / "alias-owner"
    alias_owner.symlink_to(real_owner, target_is_directory=True)
    project = alias_owner / "repo"
    store_root = project / ".agent-sentinel" / "state"
    service = SentinelService(store_root)
    session_id = "aliased-local-session"
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-test",
            "estimated_input_tokens": 20,
            "estimated_output_tokens": 5,
            "usage_confidence": "client_reported",
            "cost_confidence": "unknown",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session_id,
                "client_session_kind": "root",
                "project_dir": str(project),
                "started_at": 1_750_000_000.0,
                "updated_at": 1_750_000_000.0,
            },
        },
        trusted_usage_import=True,
    )

    projection = TestClient(create_local_api_app(store_dir=store_root)).get("/tasks").json()

    assert projection["summary"]["task_count"] == 1
    assert projection["tasks"][0]["sessions"][0]["project"] == "repo"


@pytest.mark.parametrize(
    ("identity_key", "identity_value"),
    [
        ("client_transcript_id", "foreign-transcript"),
        ("session_namespace_fingerprint", "ns:foreign"),
    ],
)
def test_project_store_quarantines_pathless_check_matching_filtered_foreign_fact(
    tmp_path: Path,
    identity_key: str,
    identity_value: str,
) -> None:
    own_project = tmp_path / "owner-a" / "repo"
    foreign_project = tmp_path / "owner-b" / "repo"
    store_root = own_project / ".agent-sentinel" / "state"
    service = SentinelService(store_root)
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "foreign-work",
                "section_status": "completed",
                "section_title": "Foreign filtered fact",
                "client": "codex",
                "project_dir": str(foreign_project),
                identity_key: identity_value,
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
                identity_key: identity_value,
                "evidence_type": "security",
                "name": f"Pathless {identity_key} probe",
                "result": "failed",
                "summary": "FOREIGN PATHLESS IDENTITY FINDING",
            },
        }
    )

    projection = TestClient(create_local_api_app(store_dir=store_root)).get("/tasks").json()

    assert projection["tasks"] == []
    assert projection["unresolved_work"] == []
    assert projection["unassigned_findings"] == []
    assert projection["summary"]["total_open_finding_count"] == 0


def test_project_store_quarantines_pathless_check_with_only_foreign_candidate_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own_project = tmp_path / "owner-a" / "repo"
    foreign_project = tmp_path / "owner-b" / "repo"
    store_root = own_project / ".agent-sentinel" / "state"
    service = SentinelService(store_root)
    foreign_session = "foreign-candidate-session"
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-test",
            "estimated_input_tokens": 20,
            "estimated_output_tokens": 5,
            "usage_confidence": "client_reported",
            "cost_confidence": "unknown",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": foreign_session,
                "client_session_kind": "root",
                "project_dir": str(foreign_project),
                "started_at": 1_750_000_000.0,
                "updated_at": 1_750_000_000.0,
            },
        },
        trusted_usage_import=True,
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "codex",
                "evidence_type": "security",
                "name": "Candidate-only foreign probe",
                "result": "failed",
                "summary": "FOREIGN CANDIDATE ONLY",
            },
        }
    )
    original_build = api_module.build_work_ledger

    def build_with_candidate(*args: object, **kwargs: object) -> dict[str, object]:
        ledger = original_build(*args, **kwargs)
        for event in ledger.get("evidence_events", []):
            if event.get("summary") == "FOREIGN CANDIDATE ONLY":
                event["log_evidence_candidate_sessions"] = [
                    {"client": "codex", "client_session_id": foreign_session}
                ]
        return ledger

    monkeypatch.setattr(api_module, "build_work_ledger", build_with_candidate)

    projection = TestClient(create_local_api_app(store_dir=store_root)).get("/tasks").json()

    assert projection["tasks"] == []
    assert projection["unassigned_findings"] == []
    assert projection["summary"]["total_open_finding_count"] == 0
