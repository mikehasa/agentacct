"""End-to-end dashboard contracts for attention-only finding dispositions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentacct.api import DASHBOARD_CSP, _finding_form_token, create_local_api_app
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


def _assigned_episode(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["tasks"][0]["finding_episodes"][0]


def _unassigned_episode(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["unassigned_findings"][0]["episode"]


def _form(
    payload: dict[str, Any],
    episode: dict[str, Any],
    *,
    action: str,
    note: str = "",
) -> dict[str, str]:
    return {
        "csrf_token": payload["csrf_token"],
        "finding_token": episode["finding_token"],
        "action": action,
        "expected_revision": str(episode["revision"]),
        "note": note,
    }


def _disposition_rows(service: SentinelService) -> list[dict[str, Any]]:
    return [
        event
        for event in service.list_all_events()
        if event.get("event_type") == "finding_disposition"
    ]


def test_finding_post_rejects_bad_parser_csrf_and_token_without_writing(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_check(service)
    client = TestClient(create_local_api_app(store_dir=store_root))
    payload = client.get("/tasks").json()
    episode = _unassigned_episode(payload)
    valid = _form(payload, episode, action="mark_reviewed")
    events_before = service.events_path.read_bytes()

    missing_csrf = dict(valid)
    missing_csrf.pop("csrf_token")
    assert client.post("/findings/disposition", data=missing_csrf).status_code == 403

    invalid_csrf = {**valid, "csrf_token": "not-issued"}
    assert client.post("/findings/disposition", data=invalid_csrf).status_code == 403

    for revision in (True, 1.0):
        response = client.post(
            "/findings/disposition",
            json={
                **valid,
                "expected_revision": revision,
            },
        )
        assert response.status_code == 422

    unknown = {**valid, "finding_token": "0" * 32}
    assert client.post("/findings/disposition", data=unknown).status_code == 404
    malformed = {**valid, "finding_token": "not-a-token"}
    assert client.post("/findings/disposition", data=malformed).status_code == 404

    duplicate = (
        f"csrf_token={payload['csrf_token']}&finding_token={episode['finding_token']}"
        "&action=mark_reviewed&action=resolve&expected_revision=0&note="
    )
    assert client.post(
        "/findings/disposition",
        content=duplicate,
        headers={"content-type": "application/x-www-form-urlencoded"},
    ).status_code == 422
    assert client.post(
        "/findings/disposition",
        json={**valid, "target_event_id": "raw-identifiers-are-not-accepted"},
    ).status_code == 422
    assert client.get("/findings/disposition").status_code == 405

    assert service.events_path.read_bytes() == events_before


def test_resolve_retry_collision_reopen_and_truth_rendering(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    session_id = "prediction-root"
    _record_root_usage(service, session_id=session_id)
    _record_check(
        service,
        session_id=session_id,
        summary="A negative buy produces an impossible share balance.",
    )
    client = TestClient(create_local_api_app(store_dir=store_root))
    initial = client.get("/tasks").json()
    episode = _assigned_episode(initial)
    form = _form(initial, episode, action="resolve", note="<tag> verified in a separate review")

    response = client.post("/findings/disposition", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["content-security-policy"] == DASHBOARD_CSP
    assert response.headers["location"] == f"/#finding-{episode['finding_token']}"
    assert len(_disposition_rows(service)) == 1

    retry = client.post("/findings/disposition", data=form, follow_redirects=False)
    assert retry.status_code == 303
    assert len(_disposition_rows(service)) == 1

    changed_note = {**form, "note": "different operation under the same derived key"}
    assert client.post(
        "/findings/disposition",
        data=changed_note,
        follow_redirects=False,
    ).status_code == 409
    assert len(_disposition_rows(service)) == 1

    resolved = client.get("/tasks").json()
    resolved_episode = _assigned_episode(resolved)
    assert resolved["summary"]["total_open_finding_count"] == 0
    assert resolved["summary"]["current_finding_count"] == 1
    assert resolved["summary"]["resolved_finding_count"] == 1
    assert resolved_episode["disposition_state"] == "resolved"
    assert resolved_episode["revision"] == 1
    assert resolved_episode["failure_event"]["result"] == "failed"
    html = client.get("/").text
    assert "Marked resolved" in html
    assert "not machine verification" in html
    assert "&lt;tag&gt; verified in a separate review" in html
    assert "<tag> verified in a separate review" not in html
    assert ">Reopen<" in html
    assert '<span class="status status-ok">Verified</span>' not in html

    public_task_id = resolved["tasks"][0]["public_task_id"]
    intelligence = client.get(f"/api/tasks/{public_task_id}").json()
    assert intelligence["states"]["outcome"]["key"] == "finding"
    assert intelligence["decision_brief"]["finding_attention_state"] == "resolved"
    assert intelligence["findings"]["current_count"] == 1
    assert intelligence["findings"]["resolved_count"] == 1
    assert intelligence["findings"]["current"][0]["result"] == "failed"
    detail_html = client.get(f"/tasks/{public_task_id}").text
    assert "Marked resolved finding" in detail_html
    assert "this is not machine verification" in detail_html
    assert "<strong>Unresolved finding</strong>" not in detail_html

    reopen = _form(resolved, resolved_episode, action="reopen")
    assert client.post(
        "/findings/disposition",
        data=reopen,
        follow_redirects=False,
    ).status_code == 303
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
    client = TestClient(create_local_api_app(store_dir=store_root))
    initial = client.get("/tasks").json()
    episode = _assigned_episode(initial)
    form = _form(initial, episode, action="resolve", note="Reviewed before the passing rerun.")

    assert client.post(
        "/findings/disposition",
        data=form,
        follow_redirects=False,
    ).status_code == 303
    _record_check(service, session_id=session_id, result="passed")
    assert client.get("/tasks").json()["tasks"][0]["finding_episodes"] == []

    retry = client.post(
        "/findings/disposition",
        data=form,
        follow_redirects=False,
    )
    assert retry.status_code == 303
    assert len(_disposition_rows(service)) == 1
    collision = client.post(
        "/findings/disposition",
        data={**form, "note": "A different payload after the pass."},
        follow_redirects=False,
    )
    assert collision.status_code == 409
    assert len(_disposition_rows(service)) == 1


@pytest.mark.parametrize("newer_result", ["passed", "failed"])
def test_stale_finding_form_after_newer_check_never_writes(
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
    old_form = _form(initial, old_episode, action="mark_reviewed")

    _record_check(service, session_id=session_id, result=newer_result)
    response = client.post(
        "/findings/disposition",
        data=old_form,
        follow_redirects=False,
    )
    assert response.status_code in {404, 409}
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


def test_linked_root_pass_racing_post_closes_target_before_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    first_session = "linked-root-a"
    second_session = "linked-root-b"
    _record_root_usage(service, session_id=first_session)
    _record_root_usage(service, session_id=second_session)
    client = TestClient(create_local_api_app(store_dir=store_root))
    before_link = client.get("/tasks").json()
    assert client.post(
        "/tasks/link",
        data={
            "csrf_token": before_link["csrf_token"],
            "client": "codex",
            "client_session_id": first_session,
            "target_client": "codex",
            "target_client_session_id": second_session,
            "confirm_cross_scope": "false",
        },
        follow_redirects=False,
    ).status_code == 303
    _record_check(service, session_id=first_session)
    current = client.get("/tasks").json()
    assert len(current["tasks"]) == 1
    episode = _assigned_episode(current)

    original = SentinelService.record_finding_disposition
    injected = False

    def inject_pass_before_lock(self: SentinelService, **kwargs: Any) -> dict[str, Any]:
        nonlocal injected
        if not injected:
            injected = True
            _record_check(service, session_id=second_session, result="passed")
        return original(self, **kwargs)

    monkeypatch.setattr(SentinelService, "record_finding_disposition", inject_pass_before_lock)
    response = client.post(
        "/findings/disposition",
        data=_form(current, episode, action="mark_reviewed"),
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert _disposition_rows(service) == []
    settled = client.get("/tasks").json()
    assert settled["tasks"][0]["finding_episodes"] == []


def test_unassigned_review_remains_visible_and_reopenable(tmp_path: Path) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_check(service, summary="An unassigned security finding remains objective evidence.")
    client = TestClient(create_local_api_app(store_dir=store_root))
    initial = client.get("/tasks").json()
    episode = _unassigned_episode(initial)

    assert client.post(
        "/findings/disposition",
        data=_form(initial, episode, action="mark_reviewed"),
        follow_redirects=False,
    ).status_code == 303
    reviewed = client.get("/tasks").json()
    assert reviewed["summary"]["total_open_finding_count"] == 0
    assert reviewed["unassigned_findings"] == []
    assert len(reviewed["disposed_unassigned_findings"]) == 1
    disposed_episode = reviewed["disposed_unassigned_findings"][0]["episode"]
    assert disposed_episode["failure_event"]["result"] == "failed"
    assert disposed_episode["disposition_state"] == "reviewed"
    html = client.get("/").text
    assert "Reviewed workspace finding" in html
    assert "Finding reviewed" in html
    assert ">Reopen<" in html

    assert client.post(
        "/findings/disposition",
        data=_form(reviewed, disposed_episode, action="reopen"),
        follow_redirects=False,
    ).status_code == 303
    reopened = client.get("/tasks").json()
    assert reopened["summary"]["total_open_finding_count"] == 1
    assert _unassigned_episode(reopened)["revision"] == 2


def test_disposition_persists_across_dashboard_restart_with_a_new_action_token(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    _record_check(service)
    first_client = TestClient(create_local_api_app(store_dir=store_root))
    initial = first_client.get("/tasks").json()
    episode = _unassigned_episode(initial)
    assert first_client.post(
        "/findings/disposition",
        data=_form(initial, episode, action="mark_reviewed"),
        follow_redirects=False,
    ).status_code == 303

    second_client = TestClient(create_local_api_app(store_dir=store_root))
    restored = second_client.get("/tasks").json()
    restored_episode = restored["disposed_unassigned_findings"][0]["episode"]
    assert restored_episode["disposition_state"] == "reviewed"
    assert restored_episode["revision"] == 1
    assert restored_episode["finding_token"] != episode["finding_token"]
    assert second_client.post(
        "/findings/disposition",
        data=_form(restored, restored_episode, action="reopen"),
        follow_redirects=False,
    ).status_code == 303
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

    client = TestClient(create_local_api_app(store_dir=store_root))
    initial = client.get("/tasks").json()
    assert initial["summary"]["unresolved_open_finding_count"] == 1
    episode = initial["unresolved_work"][0]["item"]["finding_episodes"][0]
    initial_html = client.get("/").text
    assert "Mark reviewed" in initial_html
    assert "Mark resolved" in initial_html

    assert client.post(
        "/findings/disposition",
        data=_form(initial, episode, action="mark_reviewed"),
        follow_redirects=False,
    ).status_code == 303
    reviewed = client.get("/tasks").json()
    reviewed_episode = reviewed["unresolved_work"][0]["item"]["finding_episodes"][0]
    assert reviewed["summary"]["unresolved_open_finding_count"] == 0
    assert reviewed_episode["disposition_state"] == "reviewed"
    assert reviewed_episode["failure_event"]["result"] == "failed"
    assert ">Reopen<" in client.get("/").text

    assert client.post(
        "/findings/disposition",
        data=_form(reviewed, reviewed_episode, action="reopen"),
        follow_redirects=False,
    ).status_code == 303
    reopened = client.get("/tasks").json()
    assert reopened["summary"]["unresolved_open_finding_count"] == 1
    assert reopened["unresolved_work"][0]["item"]["finding_episodes"][0]["revision"] == 2


def test_hidden_foreign_project_finding_cannot_be_dispositioned(tmp_path: Path) -> None:
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
    client = TestClient(create_local_api_app(store_dir=store_root))
    payload = client.get("/tasks").json()
    assert payload["summary"]["total_open_finding_count"] == 0

    # The form secret is intentionally available to the localhost page. Even
    # with the exact HMAC for a raw hidden row, the resolver must use only the
    # canonical scope-quarantined episode index.
    hidden_target = next(
        event
        for event in build_evidence_events(service.list_all_events())
        if event.get("summary") == "HIDDEN FOREIGN FINDING"
    )
    crafted_token = _finding_form_token(payload["csrf_token"], hidden_target)
    assert crafted_token is not None
    response = client.post(
        "/findings/disposition",
        data={
            "csrf_token": payload["csrf_token"],
            "finding_token": crafted_token,
            "action": "mark_reviewed",
            "expected_revision": "0",
            "note": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert _disposition_rows(service) == []
