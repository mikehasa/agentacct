"""Product semantics for agent-discovered findings on the Work homepage."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent_chronicle.api as api_module
from agent_chronicle.api import create_local_api_app
from agent_chronicle.service import SentinelService


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

    html = TestClient(create_local_api_app(store_dir=store_root)).get("/").text

    assert "1 task has an open finding" in html
    assert '<span class="status status-finding">Open finding</span>' in html
    assert "Agent finding" in html
    assert "A negative buy produces an impossible share balance." in html
    assert "This finding is about the work being reviewed, not agentacct health." in html
    assert "No follow-up action was recorded." in html
    assert "task needs review" not in html
    assert "What failed" not in html
    assert "What to do" not in html
    assert "Resolve the reported failure" not in html


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
    html = client.get("/").text

    assert projection["summary"]["unassigned_open_finding_count"] == 1
    assert len(projection["unassigned_findings"]) == 1
    assert projection["unassigned_findings"][0]["assignment_state"] == "no_task_candidate"
    assert "1 unassigned agent finding" in html
    assert "Workspace findings" in html
    assert "Unassigned agent finding" in html
    assert "A negative buy produces an impossible share balance." in html
    assert "Task association unavailable" in html
    assert "agentacct kept the finding visible without inventing one." in html
    assert '<strong>1</strong><span>Open findings</span>' in html
    assert '<strong>0</strong><span>Needs input</span>' in html


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
    html = client.get("/").text

    assert projection["summary"]["unassigned_open_finding_count"] == 0
    assert projection["unassigned_findings"] == []
    assert "Workspace findings" not in html
    assert '<strong>0</strong><span>Open findings</span>' in html


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
    html = client.get("/").text

    assert projection["summary"]["assigned_open_finding_count"] == 2
    assert projection["summary"]["unassigned_open_finding_count"] == 1
    assert projection["summary"]["total_open_finding_count"] == 3
    assert '<strong>3</strong><span>Open findings</span>' in html
    assert "3 open findings" in html


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
    projection = client.get("/tasks").json()
    html = client.get("/").text

    assert projection["summary"]["unassigned_open_finding_count"] == 1
    assert projection["summary"]["total_open_finding_count"] == 1
    assert [
        finding["event"]["summary"] for finding in projection["unassigned_findings"]
    ] == ["A legacy project-store row has no project path."]
    assert "Legacy local finding" in html or "A legacy project-store row has no project path." in html
    assert "This belongs to the other repo directory." not in html


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
    projection = client.get("/tasks").json()
    html = client.get("/").text

    assert projection["tasks"] == []
    assert projection["unassigned_findings"] == []
    assert projection["summary"]["total_open_finding_count"] == 0
    assert "FOREIGN ASSIGNED FINDING" not in html
    assert '<strong>0</strong><span>Open findings</span>' in html


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


def test_recent_feed_cap_never_hides_open_findings_or_blockers() -> None:
    ordinary = [
        {"name": f"ordinary-{index}", "state": {"key": "in_progress"}}
        for index in range(12)
    ]
    finding = {"name": "must-show-finding", "state": {"finding_open": True}}
    reviewed = {"name": "must-show-reviewed", "state": {"finding_present": True}}
    blocker = {"name": "must-show-blocker", "state": {"action_required": True}}

    visible = api_module._visible_attention_entries([*ordinary, finding, reviewed, blocker])
    names = {entry["name"] for entry in visible}

    assert len(visible) == 10
    assert "must-show-finding" in names
    assert "must-show-reviewed" in names
    assert "must-show-blocker" in names

    all_required = [
        {"name": f"required-{index}", "state": {"finding_open": True}}
        for index in range(12)
    ]
    assert api_module._visible_attention_entries(all_required) == all_required
