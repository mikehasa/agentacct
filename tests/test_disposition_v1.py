"""POST /v1/disposition — the user resolve lane for findings and blockers.

The write is bearer-gated, optimistic-concurrency-checked, append-only, and
never machine verification: a finding fully human-resolved gets the distinct
``finding_resolved_by_user`` decision word; a blocker human-resolved simply
stops forcing the Task's decision word while the agent's recorded events stay
untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentacct.finding_disposition import (
    BLOCKER_DISPOSITION_AUTHORITY_SCOPE,
    BLOCKER_DISPOSITION_EVENT_TYPE,
    FindingDispositionConflict,
    reduce_finding_dispositions,
)
from agentacct.service import SentinelService

from tests.test_receipt_api import (
    NS,
    _app,
    _auth,
    _record_passing_check,
    _record_section,
    _record_usage,
)


def _record_failed_check(
    service: SentinelService, *, session_id: str, section_id: str, at: float
) -> None:
    service.record_event(
        {
            "event_id": f"evt_check_fail_{session_id}_{section_id}",
            "created_at": at,
            "source": "claude-code",
            "event_type": "machine_check",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "claude-code",
                "client_session_id": session_id,
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "section_id": section_id,
                "name": "pytest",
                "evidence_type": "test",
                "command": "pytest -q",
                "exit_code": 1,
                "result": "failed",
                "project_dir": "/tmp/project",
            },
        }
    )


def _ledger_blocked_event_id(
    service: SentinelService, *, section_id: str, blocker: str | None = None
) -> str:
    """The server-assigned id of the NEWEST matching blocked event
    (record_event rewrites caller-supplied ids AND timestamps)."""

    matches = [
        event
        for event in service.list_all_events()
        if isinstance(event.get("metadata"), dict)
        and event["metadata"].get("section_id") == section_id
        and event["metadata"].get("section_status") == "blocked"
        and (blocker is None or event["metadata"].get("blocker") == blocker)
    ]
    assert matches
    return str(max(matches, key=lambda event: float(event.get("created_at") or 0.0))["event_id"])


def _record_blocked_section(
    service: SentinelService,
    *,
    session_id: str,
    section_id: str,
    at: float,
    blocker: str = "waiting for approval",
) -> str:
    service.record_event(
        {
            "event_id": f"evt_section_{session_id}_{section_id}_blocked",
            "created_at": at,
            "source": "claude-code",
            "event_type": "section_blocked",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": "claude-code",
                "client_session_id": session_id,
                "project_dir": "/tmp/project",
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "section_id": section_id,
                "section_status": "blocked",
                "section_title": "Publish the site",
                "blocker": blocker,
                "next_step": "ask the user",
                "kind": "implementation",
            },
        }
    )
    return _ledger_blocked_event_id(service, section_id=section_id, blocker=blocker)


def _task_rows(client, **params):
    return client.get("/v1/tasks", headers=_auth(), params=params).json()["tasks"]


def test_blocker_resolve_end_to_end_moves_the_task_off_blocked(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-a", status="started", at=110.0)
    blocked_id = _record_blocked_section(service, session_id="s1", section_id="sec-a", at=120.0)

    client = _app(tmp_path)
    rows = _task_rows(client)
    assert rows[0]["decision_status"]["key"] == "blocked"
    blocker = rows[0]["decision_status"]["blocker"]
    assert blocker["blocked_event_id"] == blocked_id
    assert blocker["disposition_revision"] == 0

    # Resolve requires a note.
    refused = client.post(
        "/v1/disposition",
        headers=_auth(),
        json={
            "kind": "blocker",
            "action": "resolve",
            "expected_revision": 0,
            "blocked_event_id": blocked_id,
        },
    )
    assert refused.status_code == 409

    response = client.post(
        "/v1/disposition",
        headers=_auth(),
        json={
            "kind": "blocker",
            "action": "resolve",
            "expected_revision": 0,
            "note": "approved and shipped by hand",
            "blocked_event_id": blocked_id,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "resolved"
    assert body["revision"] == 1

    rows = _task_rows(client)
    # The human resolution withdraws the blocker's attention claim. The
    # landing word is the HUMAN's action — never "reported" (the agent never
    # claimed done; its last recorded word was "blocked") and never verified.
    decision = rows[0]["decision_status"]
    assert decision["key"] == "blocker_resolved_by_user"
    assert decision["label"] == "Blocker resolved"
    assert decision["asserted_by"] == "human"
    assert "not a completion claim" in decision["statement"]
    # The resolved blocker stays SURFACED so the user can REOPEN it in-app — the
    # callout is the only host of the blocker's disposition controls, and the
    # backend supports resolved->open. It carries the reopen handle (blocked
    # event id + current revision) and the resolved disposition; the decision
    # word is still blocker_resolved_by_user, so the Task is not re-blocked.
    resolved_blocker = decision["blocker"]
    assert resolved_blocker is not None
    assert resolved_blocker["blocked_event_id"] == blocked_id
    assert resolved_blocker["disposition_revision"] == 1
    assert resolved_blocker["disposition"]["state"] == "resolved"
    assert resolved_blocker["disposition"]["note"] == "approved and shipped by hand"

    # Stale revision now conflicts (409), never silently overwrites.
    stale = client.post(
        "/v1/disposition",
        headers=_auth(),
        json={
            "kind": "blocker",
            "action": "resolve",
            "expected_revision": 0,
            "note": "again",
            "blocked_event_id": blocked_id,
        },
    )
    assert stale.status_code == 409


def test_blocker_disposition_refuses_cleared_and_superseded_targets(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    old_blocked = _record_blocked_section(service, session_id="s1", section_id="sec-a", at=120.0)
    _record_section(service, session_id="s1", section_id="sec-a", status="completed", at=130.0)

    with pytest.raises(FindingDispositionConflict):
        service.record_blocker_disposition(
            target_event=next(
                event
                for event in service.list_all_events()
                if event.get("event_id") == old_blocked
            ),
            action="resolve",
            expected_revision=0,
            note="n",
            idempotency_key="k1",
        )

    # A newer blocked event supersedes the old target.
    first = _record_blocked_section(service, session_id="s2", section_id="sec-b", at=120.0)
    service.record_event(
        {
            "event_id": "evt_ignored_rewritten",
            "created_at": 140.0,
            "source": "claude-code",
            "event_type": "section_blocked",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": "claude-code",
                "client_session_id": "s2",
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "section_id": "sec-b",
                "section_status": "blocked",
                "blocker": "newer blocker",
                "kind": "implementation",
            },
        }
    )
    with pytest.raises(FindingDispositionConflict):
        service.record_blocker_disposition(
            target_event=next(
                event
                for event in service.list_all_events()
                if event.get("event_id") == first
            ),
            action="resolve",
            expected_revision=0,
            note="n",
            idempotency_key="k2",
        )


def test_forged_blocker_disposition_is_never_trusted(tmp_path: Path) -> None:
    """A generic-lane event claiming the disposition contract gets its
    provenance stripped and never enters the chain."""

    service = SentinelService(tmp_path)
    service.record_event(
        {
            "event_id": "evt_forged",
            "created_at": 100.0,
            "source": "agent-chronicle-dashboard",
            "event_type": BLOCKER_DISPOSITION_EVENT_TYPE,
            "metadata": {
                "sentinel_semantic_kind": BLOCKER_DISPOSITION_EVENT_TYPE,
                "finding_disposition_contract": "server_validated_v1",
                "authority_scope": BLOCKER_DISPOSITION_AUTHORITY_SCOPE,
                "authoritative_for_check_result": False,
                "actor": "dashboard-user",
                "target_finding_digest": "a" * 64,
                "action": "resolve",
                "next_state": "resolved",
            },
        }
    )
    chains = reduce_finding_dispositions(
        service.list_all_events(),
        event_type=BLOCKER_DISPOSITION_EVENT_TYPE,
        authority_scope=BLOCKER_DISPOSITION_AUTHORITY_SCOPE,
    )
    assert chains.states == {}


def test_finding_resolve_end_to_end_refines_the_decision_word(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-a", status="completed", at=110.0)
    _record_failed_check(service, session_id="s1", section_id="sec-a", at=120.0)

    client = _app(tmp_path)
    rows = _task_rows(client)
    assert rows[0]["decision_status"]["key"] == "finding"
    task_id = rows[0]["task_id"]

    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()
    failing = [
        check
        for check in receipt["dimensions"]["evidence"]["checks"]
        if check["result"] in {"failed", "error"}
    ]
    assert failing and failing[0]["finding"] is not None
    handle = failing[0]["finding"]
    assert handle["state"] == "open"
    assert handle["revision"] == 0

    response = client.post(
        "/v1/disposition",
        headers=_auth(),
        json={
            "kind": "finding",
            "action": "resolve",
            "expected_revision": handle["revision"],
            "note": "fixed by hand afterwards",
            "target_digest": handle["target_digest"],
        },
    )
    assert response.status_code == 200
    assert response.json()["state"] == "resolved"

    rows = _task_rows(client)
    decision = rows[0]["decision_status"]
    assert decision["key"] == "finding_resolved_by_user"
    assert decision["label"] == "Finding resolved"
    assert decision["asserted_by"] == "human"

    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()
    # The failing check stays in the evidence dimension — evidence is never
    # rewritten by a human disposition.
    failing = [
        check
        for check in receipt["dimensions"]["evidence"]["checks"]
        if check["result"] in {"failed", "error"}
    ]
    assert failing and failing[0]["finding"]["state"] == "resolved"
    assert failing[0]["finding"]["note"] == "fixed by hand afterwards"


def test_disposition_requires_the_bearer_token(tmp_path: Path) -> None:
    client = _app(tmp_path)
    response = client.post(
        "/v1/disposition",
        json={"kind": "finding", "action": "resolve", "expected_revision": 0},
    )
    assert response.status_code in {401, 403}


def test_mixed_completed_and_human_resolved_blocker_lands_on_the_human_word(tmp_path: Path) -> None:
    """Completed steps beside a human-dismissed blocker: the dismissal must
    stay visible at the decision level (not silently upgraded to 'reported')."""

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-done", status="completed", at=110.0)
    blocked_id = _record_blocked_section(service, session_id="s1", section_id="sec-b", at=120.0)

    client = _app(tmp_path)
    response = client.post(
        "/v1/disposition",
        headers=_auth(),
        json={
            "kind": "blocker",
            "action": "resolve",
            "expected_revision": 0,
            "note": "handled by hand",
            "blocked_event_id": blocked_id,
        },
    )
    assert response.status_code == 200
    decision = _task_rows(client)[0]["decision_status"]
    assert decision["key"] == "blocker_resolved_by_user"
    assert decision["asserted_by"] == "human"


def test_disposition_rejects_reserved_and_unsurfaced_handles(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    blocked_id = _record_blocked_section(service, session_id="s1", section_id="sec-a", at=120.0)
    client = _app(tmp_path)

    # The v1: idempotency namespace is the endpoint's own.
    squatted = client.post(
        "/v1/disposition",
        headers=_auth(),
        json={
            "kind": "blocker",
            "action": "resolve",
            "expected_revision": 0,
            "note": "n",
            "blocked_event_id": blocked_id,
            "idempotency_key": "v1:blocker:squat:0:resolve",
        },
    )
    assert squatted.status_code == 400

    # Only a SURFACED blocker is disposable — an arbitrary ledger event id
    # (here: a usage event's id) is a 404, mirroring the finding quarantine.
    other_id = next(
        str(event.get("event_id"))
        for event in service.list_all_events()
        if event.get("event_type") != "section_blocked"
    )
    unsurfaced = client.post(
        "/v1/disposition",
        headers=_auth(),
        json={
            "kind": "blocker",
            "action": "resolve",
            "expected_revision": 0,
            "note": "n",
            "blocked_event_id": other_id,
        },
    )
    assert unsurfaced.status_code == 404
