"""Phase 8 integration contract for session-first Tasks.

The HTML Work page and its POST form handlers were retired; the product seam
this file protects is now the data lane that replaced them: explicit
continuation edits recorded through the continuation store, the GET /tasks
JSON projection (Task-level usage aggregation, honest ambiguous log evidence),
and the observed-task titles surfaced on GET /api/control.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.service import SentinelService
from agentacct.task_continuations import ClientSessionRef, ContinuationTaskStore


def _record_usage(
    service: SentinelService,
    *,
    session: str,
    input_tokens: int,
    output_tokens: int,
    client: str = "codex",
    session_title: str | None = None,
    session_title_trusted: bool = True,
    parent_session: str | None = None,
    session_kind: str = "root",
    cache_creation_reported: bool | None = None,
    cache_read_reported: bool | None = None,
    evidenced_event_ids: tuple[str, ...] = (),
    started_at: float = 1_750_000_000.0,
) -> dict[str, Any]:
    if cache_creation_reported is None:
        cache_creation_reported = client != "codex"
    if cache_read_reported is None:
        cache_read_reported = True
    metadata: dict[str, Any] = {
        "usage_source": "local_client_session_store",
        "client": client,
        "client_session_id": session,
        "client_session_kind": session_kind,
        "cached_input_tokens": 0,
        "cache_creation_tokens_reported": cache_creation_reported,
        "cache_read_tokens_reported": cache_read_reported,
        "started_at": started_at,
        "updated_at": started_at,
    }
    if parent_session is not None:
        metadata["parent_client_session_id"] = parent_session
    if session_title is not None:
        metadata["client_session_title"] = session_title
        if session_title_trusted:
            metadata["title_redacted"] = False
            metadata["client_session_title_source"] = "explicit_client_title_field"
            metadata["client_session_title_sanitized"] = True
        else:
            metadata["title_redacted"] = True
    if evidenced_event_ids:
        metadata["evidenced_event_ids"] = list(evidenced_event_ids)
        metadata["evidenced_event_id_total"] = len(evidenced_event_ids)
    return service.record_event(
        {
            "source": f"{client}-local-session-import",
            "event_type": "model_usage",
            "provider": client,
            "model": "gpt-phase-8",
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_cost_usd": 0.01,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "metadata": metadata,
        },
        trusted_usage_import=True,
    )


def _record_section(
    service: SentinelService,
    *,
    section_id: str,
    title: str,
    session: str | None,
    run_id: str | None = None,
    client: str = "codex",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "sentinel_semantic_kind": "section",
        "section_id": section_id,
        "section_status": "completed",
        "section_title": title,
        "client": client,
    }
    if session is not None:
        metadata["client_session_id"] = session
    return service.record_event(
        {
            "source": client,
            "event_type": "section_completed",
            "run_id": run_id,
            "metadata": metadata,
        }
    )


def _client(store_root: Path) -> TestClient:
    return TestClient(create_local_api_app(store_dir=store_root))


def _tasks(client: TestClient) -> dict[str, Any]:
    response = client.get("/tasks")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload.get("tasks"), list)
    assert isinstance(payload.get("summary"), dict)
    return payload


def _observed_task_titles(client: TestClient) -> list[str]:
    response = client.get("/api/control")
    assert response.status_code == 200
    observed = response.json().get("observed_tasks")
    assert isinstance(observed, list)
    return [str(row.get("title") or "") for row in observed]


def _session_ref(session: str, client: str = "codex") -> ClientSessionRef:
    return ClientSessionRef(client=client, client_session_id=session)


def test_link_and_unlink_merge_root_chats_and_count_each_session_usage_once(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_usage(
        service,
        session="first-root-chat",
        input_tokens=100,
        output_tokens=20,
        started_at=1_750_000_000.0,
    )
    _record_usage(
        service,
        session="continued-root-chat",
        input_tokens=80,
        output_tokens=20,
        started_at=1_750_000_100.0,
    )
    client = _client(store_root)
    before = _tasks(client)
    assert before["summary"]["task_count"] == 2

    store = ContinuationTaskStore(store_root)
    result = store.link_sessions(
        _session_ref("first-root-chat"),
        _session_ref("continued-root-chat"),
        confirmed_by="dashboard-user",
    )
    assert result.changed is True
    assert isinstance(result.task_id, str) and result.task_id.startswith("ctask_")

    linked = _tasks(client)
    assert linked["summary"]["task_count"] == 1
    task = linked["tasks"][0]
    assert task["identity_basis"] == "explicit_continuation"
    assert task["continuation_id"] == result.task_id
    assert task["session_count"] == 2
    assert task["usage"]["fresh_tokens"] == 220

    unlink_result = store.unlink_session(
        result.task_id,
        _session_ref("continued-root-chat"),
        confirmed_by="dashboard-user",
    )
    assert unlink_result.changed is True

    unlinked = _tasks(client)
    assert unlinked["summary"]["task_count"] == 2
    assert sorted(task["usage"]["fresh_tokens"] for task in unlinked["tasks"]) == [100, 120]
    assert all(task["identity_basis"] == "session_root" for task in unlinked["tasks"])


def test_rename_overrides_the_task_title(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_usage(service, session="rename-chat-a", input_tokens=30, output_tokens=5)
    _record_usage(service, session="rename-chat-b", input_tokens=20, output_tokens=5)
    _record_section(
        service,
        section_id="original-title-step",
        title="Original implementation title",
        session="rename-chat-a",
    )
    store = ContinuationTaskStore(store_root)
    linked_result = store.link_sessions(
        _session_ref("rename-chat-a"),
        _session_ref("rename-chat-b"),
        confirmed_by="dashboard-user",
    )
    assert linked_result.changed is True
    assert isinstance(linked_result.task_id, str)

    client = _client(store_root)
    linked = _tasks(client)
    assert linked["summary"]["task_count"] == 1
    task_id = linked["tasks"][0]["task_id"]
    assert linked["tasks"][0]["title_override"] is None
    assert _observed_task_titles(client) == ["Original implementation title"]

    rename_result = store.rename_task(
        linked_result.task_id,
        "Renamed migration task",
        confirmed_by="dashboard-user",
    )
    assert rename_result.changed is True

    renamed = _tasks(client)
    assert renamed["tasks"][0]["task_id"] == task_id
    assert renamed["tasks"][0]["title_override"] == "Renamed migration task"
    assert _observed_task_titles(client) == ["Renamed migration task"]


def test_claude_root_without_work_uses_explicit_client_session_title(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_usage(
        service,
        session="claude-titled-root",
        client="claude-code",
        session_title="Refine billing dashboard",
        input_tokens=12,
        output_tokens=3,
    )
    client = _client(store_root)

    projection = _tasks(client)
    assert projection["summary"]["task_count"] == 1
    assert projection["tasks"][0]["work_items"] == []

    assert _observed_task_titles(client) == ["Refine billing dashboard"]


def test_untrusted_or_redacted_session_title_never_reaches_task_surfaces(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    secret = "PRIVATE prompt-derived text"
    _record_usage(
        service,
        session="claude-untrusted-title",
        client="claude-code",
        session_title=secret,
        session_title_trusted=False,
        input_tokens=12,
        output_tokens=3,
    )
    client = _client(store_root)

    projection = _tasks(client)
    assert secret not in str(projection)

    control_payload = client.get("/api/control")
    assert control_payload.status_code == 200
    assert secret not in control_payload.text
    assert _observed_task_titles(client) == ["Untitled Claude Code chat"]


def test_run_id_steps_in_one_root_group_as_nested_work_in_one_task(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_usage(service, session="gstack-root-chat", input_tokens=120, output_tokens=30)
    _record_section(
        service,
        section_id="install-gstack",
        title="Install gstack for Codex",
        session="gstack-root-chat",
        run_id="shared-gstack-run",
    )
    _record_section(
        service,
        section_id="inspect-gstack",
        title="Inspect gstack Codex setup",
        session="gstack-root-chat",
        run_id="shared-gstack-run",
    )
    client = _client(store_root)

    projection = _tasks(client)
    assert projection["summary"]["task_count"] == 1
    task = projection["tasks"][0]
    assert len(task["work_items"]) == 2
    assert task["run_subgroups"] == [
        {
            "run_id": "shared-gstack-run",
            "work_ids": [item["work_id"] for item in task["work_items"]],
        }
    ]

    titles = _observed_task_titles(client)
    assert len(titles) == 1
    assert titles[0] in {"Install gstack for Codex", "Inspect gstack Codex setup"}


def test_common_root_ambiguous_log_donors_group_without_exact_item_attribution(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    section = _record_section(
        service,
        section_id="ambiguous-install",
        title="Install gstack from shared log evidence",
        session=None,
    )
    evidenced = (section["event_id"],)
    _record_usage(
        service,
        session="ambiguous-root-chat",
        input_tokens=50,
        output_tokens=10,
        evidenced_event_ids=evidenced,
        started_at=1_750_000_000.0,
    )
    _record_usage(
        service,
        session="ambiguous-review-chat",
        input_tokens=30,
        output_tokens=10,
        parent_session="ambiguous-root-chat",
        session_kind="internal",
        evidenced_event_ids=evidenced,
        started_at=1_750_000_100.0,
    )
    client = _client(store_root)

    projection = _tasks(client)
    assert projection["summary"]["task_count"] == 1
    task = projection["tasks"][0]
    assert task["session_count"] == 2
    assert task["usage"]["fresh_tokens"] == 60
    assert task["usage"]["excluded_non_additive_rows"] == 1
    assert len(task["work_items"]) == 1
    item = task["work_items"][0]
    assert item["client_session_id"] is None
    assert item["linked_usage_records"] == 0
    assert item["usage_fresh_total"] == 0
    association = task["work_associations"][0]
    assert association["basis"] == "common_task_from_candidate_sessions"
    assert association["client_session"] is None
    assert association["exact_session_id"] is None
    assert association["session_unlinked"] is True
