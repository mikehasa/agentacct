"""Phase 8 integration contract for session-first Tasks.

These tests intentionally exercise the HTTP boundary and the rendered Work
page together.  The lower-level continuation reducer and pure Task projection
have their own unit tests; this file protects the product seam that joins them:
ephemeral CSRF, explicit continuation edits, Task-level usage aggregation, and
honest ambiguous log evidence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_chronicle.api import DASHBOARD_CSP, create_local_api_app
from agent_chronicle.service import SentinelService
from agent_chronicle.task_continuations import (
    CONTINUATION_ACTIONS_FILENAME,
    CONTINUATION_STORE_DIRNAME,
)


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
    assert isinstance(payload.get("csrf_token"), str)
    assert len(payload["csrf_token"]) >= 32
    assert isinstance(payload.get("tasks"), list)
    assert isinstance(payload.get("summary"), dict)
    return payload


def _task_cards(html: str) -> list[str]:
    return re.findall(
        r'<article class="work-feed-item(?: [^"]*)?">.*?</article>',
        html,
        flags=re.DOTALL,
    )


def _link_form(csrf_token: str, first: str, second: str) -> dict[str, str]:
    return {
        "csrf_token": csrf_token,
        "client": "codex",
        "client_session_id": first,
        "target_client": "codex",
        "target_client_session_id": second,
        "confirm_cross_scope": "false",
    }


def test_task_mutations_reject_missing_or_invalid_csrf_without_writing(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_usage(service, session="csrf-chat-a", input_tokens=10, output_tokens=2)
    _record_usage(service, session="csrf-chat-b", input_tokens=20, output_tokens=3)
    client = _client(store_root)
    initial = _tasks(client)
    assert initial["summary"]["task_count"] == 2

    events_before = service.events_path.read_bytes()
    actions_path = store_root / CONTINUATION_STORE_DIRNAME / CONTINUATION_ACTIONS_FILENAME
    assert not actions_path.exists()

    missing = _link_form(initial["csrf_token"], "csrf-chat-a", "csrf-chat-b")
    missing.pop("csrf_token")
    missing_response = client.post("/tasks/link", data=missing, follow_redirects=False)
    assert missing_response.status_code == 403

    invalid = _link_form("not-the-issued-token", "csrf-chat-a", "csrf-chat-b")
    invalid_response = client.post("/tasks/link", data=invalid, follow_redirects=False)
    assert invalid_response.status_code == 403

    duplicate_form_response = client.post(
        "/tasks/link",
        content=(
            f"csrf_token={initial['csrf_token']}&client=codex&client=claude-code"
            "&client_session_id=csrf-chat-a&target_client=codex"
            "&target_client_session_id=csrf-chat-b&confirm_cross_scope=false"
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert duplicate_form_response.status_code == 422

    duplicate_json_response = client.post(
        "/tasks/link",
        content=(
            '{"csrf_token":"'
            + initial["csrf_token"]
            + '","client":"codex","client":"claude-code",'
            '"client_session_id":"csrf-chat-a","target_client":"codex",'
            '"target_client_session_id":"csrf-chat-b","confirm_cross_scope":false}'
        ),
        headers={"content-type": "application/json"},
        follow_redirects=False,
    )
    assert duplicate_json_response.status_code == 422

    assert service.events_path.read_bytes() == events_before
    assert not actions_path.exists()
    unchanged = _tasks(client)
    assert unchanged["tasks"] == initial["tasks"]


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

    linked_response = client.post(
        "/tasks/link",
        data=_link_form(before["csrf_token"], "first-root-chat", "continued-root-chat"),
        follow_redirects=False,
    )
    assert linked_response.status_code == 303
    assert linked_response.headers["location"].startswith("/")
    assert linked_response.headers["content-security-policy"] == DASHBOARD_CSP

    linked = _tasks(client)
    assert linked["summary"]["task_count"] == 1
    task = linked["tasks"][0]
    assert task["identity_basis"] == "explicit_continuation"
    assert task["session_count"] == 2
    assert task["usage"]["fresh_tokens"] == 220

    linked_html = client.get("/").text
    linked_cards = _task_cards(linked_html)
    assert len(linked_cards) == 1
    assert "2 chats linked" in linked_cards[0]
    assert linked_cards[0].count("220 total tokens") == 1

    unlinked_response = client.post(
        "/tasks/unlink",
        data={
            "csrf_token": linked["csrf_token"],
            "client": "codex",
            "client_session_id": "continued-root-chat",
        },
        follow_redirects=False,
    )
    assert unlinked_response.status_code == 303
    assert unlinked_response.headers["content-security-policy"] == DASHBOARD_CSP

    unlinked = _tasks(client)
    assert unlinked["summary"]["task_count"] == 2
    assert sorted(task["usage"]["fresh_tokens"] for task in unlinked["tasks"]) == [100, 120]
    unlinked_cards = _task_cards(client.get("/").text)
    assert len(unlinked_cards) == 2
    assert all("2 chats linked" not in card for card in unlinked_cards)


def test_rename_overrides_the_task_card_title(tmp_path: Path) -> None:
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
    client = _client(store_root)
    initial = _tasks(client)
    response = client.post(
        "/tasks/link",
        data=_link_form(initial["csrf_token"], "rename-chat-a", "rename-chat-b"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    linked = _tasks(client)
    task_id = linked["tasks"][0]["task_id"]

    renamed_response = client.post(
        "/tasks/rename",
        data={
            "csrf_token": linked["csrf_token"],
            "task_id": task_id,
            "title": "Renamed migration task",
        },
        follow_redirects=False,
    )
    assert renamed_response.status_code == 303
    assert renamed_response.headers["content-security-policy"] == DASHBOARD_CSP

    renamed = _tasks(client)
    assert renamed["tasks"][0]["task_id"] == task_id
    assert renamed["tasks"][0]["title_override"] == "Renamed migration task"
    headings = re.findall(
        r'<h3 class="work-feed-title"><a class="task-title-link" href="/tasks/task_[0-9a-f]{32}">([^<]+)</a></h3>',
        client.get("/").text,
    )
    assert headings == ["Renamed migration task"]


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

    cards = _task_cards(client.get("/").text)
    assert len(cards) == 1
    assert re.search(
        r'<h3 class="work-feed-title"><a class="task-title-link" href="/tasks/task_[0-9a-f]{32}">Refine billing dashboard</a></h3>',
        cards[0],
    )
    assert "Claude Code task" not in cards[0]


def test_untrusted_or_redacted_session_title_never_reaches_tasks_or_home(tmp_path: Path) -> None:
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
    home = client.get("/").text

    assert secret not in str(projection)
    assert secret not in home
    assert re.search(
        r'<h3 class="work-feed-title"><a class="task-title-link" href="/tasks/task_[0-9a-f]{32}">Untitled Claude Code chat</a></h3>',
        home,
    )


def test_run_id_steps_in_one_root_render_as_nested_work_in_one_task(tmp_path: Path) -> None:
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

    cards = _task_cards(client.get("/").text)
    assert len(cards) == 1
    card = cards[0]
    details_at = card.index('<details class="work-feed-why">')
    assert card.index("Install gstack for Codex") < details_at
    assert card.index("Inspect gstack Codex setup") < details_at
    assert "2 work steps" in card


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

    cards = _task_cards(client.get("/").text)
    assert len(cards) == 1
    assert "Install gstack from shared log evidence" in cards[0]
    assert "60 known subtotal tokens" in cards[0]
    assert "1 usage row held" in cards[0]
    assert "attributed usage" not in cards[0]
