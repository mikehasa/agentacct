"""End-to-end contract from mechanical hooks to the product Task surface."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentacct import api as api_module
from agentacct.api import create_local_api_app
from agentacct.evidence_runtime import EvidenceRuntime
from agentacct.evidence_store import EVIDENCE_STORE_DIRNAME
from agentacct.finding_disposition import FindingDispositionConflict
from agentacct.service import SentinelService
from agentacct.task_outcome import reduce_task_outcome


def _codex_payload(
    session_id: str,
    *,
    timestamp: str,
    parent_session_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hook_event_name": "Stop",
        "timestamp": timestamp,
        "session_id": session_id,
        "turn_id": f"turn-{session_id}",
        "model": "gpt-5",
        "status": "completed",
    }
    if parent_session_id is not None:
        payload["parent_session_id"] = parent_session_id
    return payload


def _cursor_check_payload(
    session_id: str,
    *,
    timestamp: str,
    exit_code: int,
) -> dict[str, Any]:
    return {
        "hook_event_name": "afterShellExecution",
        "timestamp": timestamp,
        "conversation_id": session_id,
        "session_id": f"process-{session_id}",
        "generation_id": f"generation-{timestamp}",
        "command": "npm run typecheck",
        "exit_code": exit_code,
        "output": "PRIVATE_OUTPUT_CANARY",
        "prompt": "PRIVATE_PROMPT_CANARY",
    }


def _record_usage_and_work(store: Path, session_id: str) -> None:
    service = SentinelService(store)
    service.record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "openai",
            "model": "gpt-5",
            "estimated_input_tokens": 120,
            "estimated_output_tokens": 30,
            "estimated_cost_usd": 0.02,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": session_id,
                "client_session_kind": "root",
                "cached_input_tokens": 0,
                "cache_creation_tokens_reported": False,
                "cache_read_tokens_reported": True,
                "started_at": 1_784_000_000.0,
                "updated_at": 1_784_000_010.0,
            },
        },
        trusted_usage_import=True,
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "created_at": 1_784_000_010.0,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "bridge-work",
                "section_status": "completed",
                "section_title": "Build the session bridge",
                "client": "codex",
                "client_session_id": session_id,
            },
        }
    )


def test_capture_only_session_appears_as_observed_without_usage_claim(tmp_path: Path) -> None:
    store = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store))
    session_id = "capture-only-codex-session"

    response = client.post(
        "/capture/codex/Stop",
        json=_codex_payload(session_id, timestamp="2026-07-16T08:00:00Z"),
    )
    assert response.status_code == 200
    assert response.json()["stored_count"] == 1

    projection = client.get("/tasks").json()
    assert projection["summary"]["task_count"] == 1
    task = projection["tasks"][0]
    assert len(task["sessions"]) == 1
    assert task["usage"]["rows"] == 0
    assert task["usage"]["estimated_cost_usd"] is None
    assert task["models"] == ["gpt-5"]
    assert task["usage"]["model_lanes"] == []
    sessions_payload = client.get("/sessions").json()
    mechanical_health = sessions_payload["summary"]["mechanical_projection"]
    assert mechanical_health["window_limit"] == 10_000
    assert mechanical_health["history_window_maybe_truncated"] == 0

    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "observed"
    assert outcome["verification"] is None


def test_capture_parent_child_sessions_fold_into_one_task(tmp_path: Path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    root = "codex-root-session"
    child = "codex-child-session"

    assert client.post(
        "/capture/codex/Stop",
        json=_codex_payload(root, timestamp="2026-07-16T08:00:00Z"),
    ).status_code == 200
    assert client.post(
        "/capture/codex/Stop",
        json=_codex_payload(child, timestamp="2026-07-16T08:01:00Z", parent_session_id=root),
    ).status_code == 200

    projection = client.get("/tasks").json()
    assert projection["summary"]["task_count"] == 1
    assert projection["summary"]["session_count"] == 2
    task = projection["tasks"][0]
    assert len(task["sessions"]) == 2
    assert task["primary_root"] == {"client": "codex", "client_session_id": root}


def test_missing_host_timestamp_retry_remains_one_visible_session(tmp_path: Path) -> None:
    store = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store))
    payload = {
        "hook_event_name": "Stop",
        "session_id": "retry-without-host-time",
        "turn_id": "retry-turn",
        "model": "gpt-5",
    }

    first = client.post("/capture/codex/Stop", json=payload)
    second = client.post("/capture/codex/Stop", json=payload)
    assert first.status_code == second.status_code == 200

    projection = client.get("/tasks").json()
    assert projection["summary"]["task_count"] == 1
    assert projection["summary"]["session_count"] == 1
    assert projection["tasks"][0]["models"] == ["gpt-5"]
    diagnostics = client.get("/sessions").json()["summary"]["mechanical_projection"]
    assert diagnostics["timestamp_retry_groups_collapsed"] == 1


def test_conflict_repair_has_page_wide_query_and_row_budgets(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    for index in range(5):
        payload = {
            "hook_event_name": "Stop",
            "session_id": f"bounded-conflict-{index}",
            "turn_id": f"bounded-turn-{index}",
            "model": "gpt-5",
        }
        assert client.post("/capture/codex/Stop", json=payload).status_code == 200
        assert client.post("/capture/codex/Stop", json=payload).status_code == 200

    monkeypatch.setattr(api_module, "MECHANICAL_CONFLICT_GROUP_LIMIT", 2)
    monkeypatch.setattr(api_module, "MECHANICAL_CONFLICT_ROW_LIMIT", 3)
    original_records = EvidenceRuntime.records
    conflict_queries: list[tuple[str, int]] = []

    def counted_records(
        runtime: EvidenceRuntime,
        *,
        limit: int = 10_000,
        descending: bool = False,
        **filters: Any,
    ) -> list[Any]:
        idempotency_key = filters.get("idempotency_key")
        if isinstance(idempotency_key, str):
            conflict_queries.append((idempotency_key, limit))
        return original_records(
            runtime,
            limit=limit,
            descending=descending,
            **filters,
        )

    monkeypatch.setattr(EvidenceRuntime, "records", counted_records)

    payload = client.get("/sessions").json()
    diagnostics = payload["summary"]["mechanical_projection"]
    assert len(conflict_queries) == 2
    assert [limit for _key, limit in conflict_queries] == [3, 1]
    assert diagnostics["conflict_groups_discovered"] == 5
    assert diagnostics["conflict_groups_queried"] == 2
    assert diagnostics["conflict_groups_expanded"] == 1
    assert diagnostics["conflict_groups_incomplete"] == 4
    assert diagnostics["conflict_groups_skipped_by_budget"] == 3
    assert diagnostics["conflict_rows_read"] == 3


def test_usage_mcp_and_hook_coalesce_without_recounting(tmp_path: Path) -> None:
    store = tmp_path / "project" / ".agent-sentinel" / "state"
    session_id = "coalesced-codex-session"
    _record_usage_and_work(store, session_id)
    client = TestClient(create_local_api_app(store_dir=store))

    assert client.post(
        "/capture/codex/Stop",
        json=_codex_payload(session_id, timestamp="2026-07-16T08:00:00Z"),
    ).status_code == 200

    projection = client.get("/tasks").json()
    assert projection["summary"]["task_count"] == 1
    assert projection["summary"]["session_count"] == 1
    task = projection["tasks"][0]
    assert len(task["work_items"]) == 1
    assert task["usage"]["rows"] == 1
    assert task["usage"]["total_tokens"] == 150
    assert task["models"] == ["gpt-5"]
    assert task["sessions"][0]["mechanical_capture"]["observation_count"] == 1


def test_completed_task_with_task_level_pass_reduces_to_verified(tmp_path: Path) -> None:
    store = tmp_path / "project" / ".agent-sentinel" / "state"
    session_id = "verified-codex-session"
    _record_usage_and_work(store, session_id)
    client = TestClient(create_local_api_app(store_dir=store))
    work = client.get("/tasks").json()["tasks"][0]
    work_updated_at = max(float(item["updated_at"]) for item in work["work_items"])
    check_timestamp = datetime.fromtimestamp(work_updated_at + 1, tz=UTC).isoformat().replace("+00:00", "Z")

    response = client.post(
        "/capture/codex/PostToolUse",
        json={
            "hook_event_name": "PostToolUse",
            "timestamp": check_timestamp,
            "session_id": session_id,
            "turn_id": "verified-turn",
            "tool_call_id": "verified-tool",
            "tool_name": "shell",
            "command": "npm run typecheck",
            "exit_code": 0,
        },
    )
    assert response.status_code == 200

    task = client.get("/tasks").json()["tasks"][0]
    assert task["task_evidence_events"][0]["result"] == "passed"

    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "verified"
    assert outcome["verification"] is not None


def test_failed_hook_check_is_finding_not_user_action(tmp_path: Path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    response = client.post(
        "/capture/cursor/afterShellExecution",
        json=_cursor_check_payload(
            "cursor-check-session",
            timestamp="2026-07-16T08:00:00Z",
            exit_code=2,
        ),
    )
    assert response.status_code == 200
    assert response.json()["stored_count"] == 2

    task = client.get("/tasks").json()["tasks"][0]
    assert len(task["task_evidence_events"]) == 1
    assert task["task_evidence_events"][0]["result"] == "failed"

    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "finding"
    assert outcome["finding_attention_state"] == "open"

    projection_text = client.get("/tasks").text
    assert "PRIVATE_OUTPUT_CANARY" not in projection_text
    assert "PRIVATE_PROMPT_CANARY" not in projection_text


def test_failed_hook_finding_can_be_reviewed_attention_only(tmp_path: Path) -> None:
    store = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store))
    assert client.post(
        "/capture/cursor/afterShellExecution",
        json=_cursor_check_payload(
            "cursor-review-session",
            timestamp="2026-07-16T08:00:00Z",
            exit_code=2,
        ),
    ).status_code == 200

    initial = client.get("/tasks").json()
    episode = initial["tasks"][0]["finding_episodes"][0]
    SentinelService(store).record_finding_disposition(
        target_event=episode["failure_event"],
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="review-cursor-finding",
    )
    # A replay of the exact same operation must not double-record.
    SentinelService(store).record_finding_disposition(
        target_event=episode["failure_event"],
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="review-cursor-finding",
    )

    reviewed = client.get("/tasks").json()
    task = reviewed["tasks"][0]
    reviewed_episode = task["finding_episodes"][0]
    assert reviewed["summary"]["total_open_finding_count"] == 0
    assert reviewed_episode["disposition_state"] == "reviewed"
    assert reviewed_episode["failure_event"]["result"] == "failed"
    assert task["task_evidence_events"][0]["result"] == "failed"
    assert len(
        [
            event
            for event in SentinelService(store).list_all_events()
            if event.get("event_type") == "finding_disposition"
        ]
    ) == 1
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "finding"
    assert outcome["finding_attention_state"] == "reviewed"

    assert client.post(
        "/capture/cursor/afterShellExecution",
        json=_cursor_check_payload(
            "cursor-review-session",
            timestamp="2026-07-16T08:01:00Z",
            exit_code=0,
        ),
    ).status_code == 200
    closed = client.get("/tasks").json()
    assert closed["tasks"][0]["finding_episodes"] == []
    with pytest.raises(FindingDispositionConflict, match="closed by newer evidence"):
        SentinelService(store).record_finding_disposition(
            target_event=episode["failure_event"],
            action="reopen",
            expected_revision=1,
            note=None,
            idempotency_key="stale-client-hook-reopen",
        )
    assert len(
        [
            event
            for event in SentinelService(store).list_all_events()
            if event.get("event_type") == "finding_disposition"
        ]
    ) == 1


def test_missing_timestamp_hook_retry_keeps_visible_finding_dispositionable(
    tmp_path: Path,
) -> None:
    store = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store))
    payload = _cursor_check_payload(
        "cursor-missing-time-review",
        timestamp="2026-07-16T08:00:00Z",
        exit_code=2,
    )
    payload.pop("timestamp")
    assert client.post("/capture/cursor/afterShellExecution", json=payload).status_code == 200
    assert client.post("/capture/cursor/afterShellExecution", json=payload).status_code == 200

    initial = client.get("/tasks").json()
    assert initial["summary"]["total_open_finding_count"] == 1
    episode = initial["tasks"][0]["finding_episodes"][0]
    SentinelService(store).record_finding_disposition(
        target_event=episode["failure_event"],
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="review-missing-time-finding",
    )
    reviewed = client.get("/tasks").json()
    assert reviewed["summary"]["total_open_finding_count"] == 0
    reviewed_episode = reviewed["tasks"][0]["finding_episodes"][0]
    assert reviewed_episode["disposition_state"] == "reviewed"
    assert reviewed_episode["failure_event"]["result"] == "failed"
    assert len(
        [
            event
            for event in SentinelService(store).list_all_events()
            if event.get("event_type") == "finding_disposition"
        ]
    ) == 1


def test_passing_only_hook_check_stays_observed_and_clears_earlier_finding(tmp_path: Path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    session_id = "cursor-retry-session"
    for timestamp, exit_code in (
        ("2026-07-16T08:00:00Z", 2),
        ("2026-07-16T08:01:00Z", 0),
    ):
        response = client.post(
            "/capture/cursor/afterShellExecution",
            json=_cursor_check_payload(session_id, timestamp=timestamp, exit_code=exit_code),
        )
        assert response.status_code == 200

    task = client.get("/tasks").json()["tasks"][0]
    checks = task["task_evidence_events"]
    assert len(checks) == 2
    assert len({check["check_identity"] for check in checks}) == 1
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "observed"
    assert outcome["finding"] is None
    assert outcome["verification"] is None


def test_task_scope_evidence_ref_and_run_hints_refuse_foreign_namespace() -> None:
    projection = {
        "tasks": [
            {
                "task_id": "session:codex:root",
                "session_keys": [
                    {"client": "codex", "client_session_id": "root"}
                ],
                "sessions": [
                    {
                        "client": "codex",
                        "client_session_id": "root",
                        "namespace_fingerprint": "ns:project-a",
                        "identity_scope_state": "explicit",
                    }
                ],
                "work_items": [
                    {
                        "work_id": "shared-work",
                        "section_id": "shared-section",
                        "client": "codex",
                        "client_session_id": "root",
                        "project_dir": "/work/project",
                        "run_id": "shared-run",
                        "session_namespace_fingerprint": "ns:project-a",
                        "identity_scope_state": "explicit",
                        "evidence_events": [],
                    }
                ],
            }
        ]
    }

    def check(
        event_id: str,
        namespace: str,
        *,
        work_id: str | None,
        run_id: str | None,
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "source": "codex",
            "source_type": "mcp_agent_reported",
            "client": "codex",
            "client_session_id": "root",
            "project_dir": "/work/project",
            "work_id": work_id,
            "section_id": None,
            "run_id": run_id,
            "session_namespace_fingerprint": namespace,
            "identity_scope_state": "explicit",
            "result": "failed",
        }

    attached = api_module._attach_evidence_to_task_projection(
        projection,
        [
            check("foreign-ref", "ns:project-b", work_id="shared-work", run_id=None),
            check("foreign-run", "ns:project-b", work_id=None, run_id="shared-run"),
            check("matching-ref", "ns:project-a", work_id="shared-work", run_id=None),
        ],
        require_namespace_for_client_hook=True,
    )

    assert [
        event["event_id"]
        for event in attached["tasks"][0]["task_evidence_events"]
    ] == ["matching-ref"]


def test_work_reads_do_not_create_or_mutate_advanced_evidence(tmp_path: Path) -> None:
    fresh_store = tmp_path / "fresh" / "state"
    fresh_client = TestClient(create_local_api_app(store_dir=fresh_store))
    assert not (fresh_store / EVIDENCE_STORE_DIRNAME).exists()
    assert fresh_client.get("/overview").status_code == 200
    assert fresh_client.get("/tasks").status_code == 200
    assert not (fresh_store / EVIDENCE_STORE_DIRNAME).exists()

    captured_store = tmp_path / "captured" / "state"
    client = TestClient(create_local_api_app(store_dir=captured_store))
    assert client.post(
        "/capture/codex/Stop",
        json=_codex_payload("advanced-stability", timestamp="2026-07-16T08:00:00Z"),
    ).status_code == 200
    before = client.get("/evidence/status").json()["stats"]
    assert client.get("/overview").status_code == 200
    assert client.get("/tasks").status_code == 200
    after = client.get("/evidence/status").json()["stats"]
    assert after == before
