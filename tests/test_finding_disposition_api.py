"""GET /tasks projection contracts for attention-only finding dispositions.

The HTML dashboard and its POST /findings/disposition form flow are retired.
Dispositions now arrive as recorded events via
``SentinelService.record_finding_disposition``; these tests cover how the kept
JSON ``GET /tasks`` projection reflects those transitions (lanes, summary
counts, revisions, tokens, restarts, and scope quarantine).  Pure disposition
semantics (idempotency, transitions, corruption) live in
``tests/test_finding_disposition.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.finding_disposition import FindingDispositionConflict
from agentacct.service import SentinelService
from agentacct.work_ledger import build_evidence_events


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
                "client_session_title": "Review prediction engine",
                "client_session_title_source": "explicit_client_title_field",
                "client_session_title_sanitized": True,
                "title_redacted": False,
                "cache_creation_tokens_reported": False,
                "cache_read_tokens_reported": True,
                "started_at": 1_750_000_000.0,
                "updated_at": 1_750_000_001.0,
            },
        },
        trusted_usage_import=True,
    )


def _record_check(
    service: SentinelService,
    *,
    name: str = "Share math boundary probe",
    result: str = "failed",
    session_id: str | None = None,
    section_id: str | None = None,
    project_dir: str | None = None,
    namespace: str | None = None,
    summary: str | None = None,
) -> None:
    metadata: dict[str, Any] = {
        "sentinel_semantic_kind": "evidence",
        "client": "codex",
        "evidence_type": "security",
        "name": name,
        "result": result,
        "summary": summary or f"{name}: {result}",
    }
    if session_id is not None:
        metadata["client_session_id"] = session_id
    if section_id is not None:
        metadata["section_id"] = section_id
    if project_dir is not None:
        metadata["project_dir"] = project_dir
    if namespace is not None:
        metadata["client_context_keys_authored"] = ["client_session_id"]
        metadata["session_namespace_fingerprint"] = namespace
        metadata["identity_scope_state"] = "explicit"
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": metadata,
        }
    )


def _latest_failed_target(service: SentinelService) -> dict[str, Any]:
    failures = [
        event
        for event in build_evidence_events(service.list_all_events())
        if event.get("result") == "failed"
    ]
    assert failures
    return failures[-1]


def _assigned_episode(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["tasks"][0]["finding_episodes"][0]


def _unassigned_episode(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["unassigned_findings"][0]["episode"]


def _disposition_rows(service: SentinelService) -> list[dict[str, Any]]:
    return [
        event
        for event in service.list_all_events()
        if event.get("event_type") == "finding_disposition"
    ]


def test_resolve_retry_collision_reopen_and_truth_projection(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session_id = "prediction-root"
    _record_root_usage(service, session_id=session_id)
    _record_check(
        service,
        session_id=session_id,
        summary="A negative buy produces an impossible share balance.",
    )
    target = _latest_failed_target(service)
    note = "<tag> verified in a separate review"

    first = service.record_finding_disposition(
        target_event=target,
        action="resolve",
        expected_revision=0,
        note=note,
        idempotency_key="op-resolve",
    )
    assert len(_disposition_rows(service)) == 1

    retry = service.record_finding_disposition(
        target_event=target,
        action="resolve",
        expected_revision=0,
        note=note,
        idempotency_key="op-resolve",
    )
    assert retry["event_id"] == first["event_id"]
    assert len(_disposition_rows(service)) == 1

    with pytest.raises(FindingDispositionConflict):
        service.record_finding_disposition(
            target_event=target,
            action="resolve",
            expected_revision=0,
            note="different operation under the same derived key",
            idempotency_key="op-resolve",
        )
    assert len(_disposition_rows(service)) == 1

    client = TestClient(create_local_api_app(store_dir=store_root))
    resolved = client.get("/tasks").json()
    resolved_episode = _assigned_episode(resolved)
    assert resolved["summary"]["total_open_finding_count"] == 0
    assert resolved["summary"]["current_finding_count"] == 1
    assert resolved["summary"]["resolved_finding_count"] == 1
    assert resolved_episode["disposition_state"] == "resolved"
    assert resolved_episode["revision"] == 1
    assert resolved_episode["failure_event"]["result"] == "failed"
    # The user note is carried verbatim as data; escaping is the consumer's job.
    assert resolved_episode["latest_disposition"]["note"] == note

    service.record_finding_disposition(
        target_event=target,
        action="reopen",
        expected_revision=1,
        note=None,
        idempotency_key="op-reopen",
    )
    reopened = client.get("/tasks").json()
    assert reopened["summary"]["total_open_finding_count"] == 1
    assert _assigned_episode(reopened)["disposition_state"] == "open"
    assert _assigned_episode(reopened)["revision"] == 2
    assert len(_disposition_rows(service)) == 2


def test_exact_retry_survives_later_pass_without_reopening_raw_resolution(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session_id = "retry-after-pass-root"
    _record_root_usage(service, session_id=session_id)
    _record_check(service, session_id=session_id)
    target = _latest_failed_target(service)

    first = service.record_finding_disposition(
        target_event=target,
        action="resolve",
        expected_revision=0,
        note="Reviewed before the passing rerun.",
        idempotency_key="op-resolve",
    )
    _record_check(service, session_id=session_id, result="passed")
    client = TestClient(create_local_api_app(store_dir=store_root))
    assert client.get("/tasks").json()["tasks"][0]["finding_episodes"] == []

    retry = service.record_finding_disposition(
        target_event=target,
        action="resolve",
        expected_revision=0,
        note="Reviewed before the passing rerun.",
        idempotency_key="op-resolve",
    )
    assert retry["event_id"] == first["event_id"]
    assert len(_disposition_rows(service)) == 1

    with pytest.raises(FindingDispositionConflict):
        service.record_finding_disposition(
            target_event=target,
            action="resolve",
            expected_revision=0,
            note="A different payload after the pass.",
            idempotency_key="op-resolve",
        )
    assert len(_disposition_rows(service)) == 1
    assert client.get("/tasks").json()["tasks"][0]["finding_episodes"] == []


@pytest.mark.parametrize("newer_result", ["passed", "failed"])
def test_stale_disposition_after_newer_check_never_writes(
    tmp_path: Path,
    newer_result: str,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session_id = "stale-root"
    _record_root_usage(service, session_id=session_id)
    _record_check(service, session_id=session_id)
    client = TestClient(create_local_api_app(store_dir=store_root))
    initial = client.get("/tasks").json()
    old_episode = _assigned_episode(initial)
    old_target = _latest_failed_target(service)

    _record_check(service, session_id=session_id, result=newer_result)
    with pytest.raises(FindingDispositionConflict):
        service.record_finding_disposition(
            target_event=old_target,
            action="mark_reviewed",
            expected_revision=0,
            note=None,
            idempotency_key="op-stale",
        )
    assert _disposition_rows(service) == []

    current = client.get("/tasks").json()
    if newer_result == "passed":
        assert current["summary"]["total_open_finding_count"] == 0
        assert current["tasks"][0]["finding_episodes"] == []
    else:
        replacement = _assigned_episode(current)
        assert replacement["finding_token"] != old_episode["finding_token"]
        assert replacement["revision"] == 0
        assert replacement["disposition_state"] == "open"


def test_unassigned_review_remains_visible_and_reopenable(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_check(service, summary="An unassigned security finding remains objective evidence.")
    target = _latest_failed_target(service)
    client = TestClient(create_local_api_app(store_dir=store_root))
    initial = client.get("/tasks").json()
    assert _unassigned_episode(initial)["disposition_state"] == "open"

    service.record_finding_disposition(
        target_event=target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="op-review",
    )
    reviewed = client.get("/tasks").json()
    assert reviewed["summary"]["total_open_finding_count"] == 0
    assert reviewed["unassigned_findings"] == []
    assert len(reviewed["disposed_unassigned_findings"]) == 1
    disposed_episode = reviewed["disposed_unassigned_findings"][0]["episode"]
    assert disposed_episode["failure_event"]["result"] == "failed"
    assert disposed_episode["disposition_state"] == "reviewed"

    service.record_finding_disposition(
        target_event=target,
        action="reopen",
        expected_revision=1,
        note=None,
        idempotency_key="op-reopen",
    )
    reopened = client.get("/tasks").json()
    assert reopened["summary"]["total_open_finding_count"] == 1
    assert _unassigned_episode(reopened)["revision"] == 2


def test_disposition_persists_across_dashboard_restart_with_a_new_action_token(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_check(service)
    target = _latest_failed_target(service)
    first_client = TestClient(create_local_api_app(store_dir=store_root))
    initial = first_client.get("/tasks").json()
    episode = _unassigned_episode(initial)
    service.record_finding_disposition(
        target_event=target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="op-review",
    )

    second_client = TestClient(create_local_api_app(store_dir=store_root))
    restored = second_client.get("/tasks").json()
    restored_episode = restored["disposed_unassigned_findings"][0]["episode"]
    assert restored_episode["disposition_state"] == "reviewed"
    assert restored_episode["revision"] == 1
    # Action tokens are derived from a per-process secret: a restarted
    # dashboard must issue fresh ones for the same underlying finding.
    assert restored_episode["finding_token"] != episode["finding_token"]

    service.record_finding_disposition(
        target_event=target,
        action="reopen",
        expected_revision=1,
        note=None,
        idempotency_key="op-reopen",
    )
    reopened = second_client.get("/tasks").json()
    assert _unassigned_episode(reopened)["disposition_state"] == "open"
    assert _unassigned_episode(reopened)["revision"] == 2


def test_unresolved_work_finding_controls_round_trip(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session_id = "namespace-mismatch-root"
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
                "session_namespace_fingerprint": "ns:root",
                "identity_scope_state": "explicit",
                "cache_creation_tokens_reported": False,
                "cache_read_tokens_reported": True,
                "started_at": 1_750_000_000.0,
                "updated_at": 1_750_000_001.0,
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
                "client": "codex",
                "client_session_id": session_id,
                "client_context_keys_authored": ["client_session_id"],
                "session_namespace_fingerprint": "ns:unresolved",
                "identity_scope_state": "explicit",
                "section_id": "unresolved-review",
                "section_status": "completed",
                "section_title": "Review unresolved namespace work",
            },
        }
    )
    _record_check(
        service,
        session_id=session_id,
        section_id="unresolved-review",
        namespace="ns:unresolved",
        summary="The unresolved work check remains failed.",
    )
    target = _latest_failed_target(service)

    client = TestClient(create_local_api_app(store_dir=store_root))
    initial = client.get("/tasks").json()
    assert initial["summary"]["unresolved_open_finding_count"] == 1
    assert initial["unresolved_work"][0]["item"]["finding_episodes"][0]["disposition_state"] == "open"

    service.record_finding_disposition(
        target_event=target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="op-review",
    )
    reviewed = client.get("/tasks").json()
    reviewed_episode = reviewed["unresolved_work"][0]["item"]["finding_episodes"][0]
    assert reviewed["summary"]["unresolved_open_finding_count"] == 0
    assert reviewed_episode["disposition_state"] == "reviewed"
    assert reviewed_episode["failure_event"]["result"] == "failed"

    service.record_finding_disposition(
        target_event=target,
        action="reopen",
        expected_revision=1,
        note=None,
        idempotency_key="op-reopen",
    )
    reopened = client.get("/tasks").json()
    assert reopened["summary"]["unresolved_open_finding_count"] == 1
    assert reopened["unresolved_work"][0]["item"]["finding_episodes"][0]["revision"] == 2


def test_hidden_foreign_project_finding_is_scope_quarantined(tmp_path: Path) -> None:
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
                "updated_at": 1_750_000_001.0,
            },
        },
        trusted_usage_import=True,
    )
    _record_check(service, session_id=session_id, summary="HIDDEN FOREIGN FINDING")

    # The raw failed check exists in the evidence stream ...
    hidden = [
        event
        for event in build_evidence_events(service.list_all_events())
        if event.get("summary") == "HIDDEN FOREIGN FINDING"
    ]
    assert len(hidden) == 1

    # ... but the /tasks projection quarantines the foreign-project scope, so
    # no open finding (nor its action token) is ever surfaced for it.
    client = TestClient(create_local_api_app(store_dir=store_root))
    payload = client.get("/tasks").json()
    assert payload["summary"]["total_open_finding_count"] == 0
    assert payload["unassigned_findings"] == []
