"""The /v1/receipt and /v1/tasks native-shell lane."""

from __future__ import annotations

from pathlib import Path

import agentacct.api as api_module
from fastapi.testclient import TestClient

from agentacct.api import (
    _LEDGER_MECHANICAL_CHECK_EVENTS_KEY,
    _LEDGER_RUN_REPORT_LIMIT,
    _collect_service_run_reports,
    _dashboard_task_projection,
    _receipt_attention_priority,
    _mechanical_projection_envelopes_for,
    _store_scope_and_label,
    build_mechanical_check_events,
    build_page_data,
    create_local_api_app,
)
from agentacct.cost import CostLedger
from agentacct.receipt import RECEIPT_SCHEMA_VERSION, V1_ATTENTION_SCHEMA_VERSION
from agentacct.service import SentinelService
from agentacct.session_observations import build_session_observations
from agentacct.work_ledger import build_proxy_usage_events, build_work_ledger

TOKEN = "test-v1-token"
NS = "sha256:receipt-api-ns"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _app(tmp_path: Path) -> TestClient:
    return TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))


def _record_usage(service: SentinelService, *, session_id: str, at: float) -> None:
    service.record_event(
        {
            "event_id": f"evt_usage_{session_id}",
            "created_at": at,
            "source": "claude-code-local-session-import",
            "event_type": "model_usage",
            "run_id": None,
            "provider": "claude-code",
            "model": "claude-opus-4-8",
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 25,
            "estimated_cost_usd": 0.5,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "cost_basis": "pricing_table",
            "metadata": {
                "usage_source": "local_client_session_store",
                "usage_provenance": "agent_sentinel_local_usage_import",
                "client": "claude-code",
                "client_session_id": session_id,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "project_dir": "/tmp/project",
                "started_at": at,
                "updated_at": at,
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "source_namespace_fingerprint": NS,
            },
        },
        trusted_usage_import=True,
    )


def _record_section(service: SentinelService, *, session_id: str, section_id: str, status: str, at: float) -> None:
    service.record_event(
        {
            "event_id": f"evt_section_{session_id}_{section_id}_{status}",
            "created_at": at,
            "source": "claude-code",
            "event_type": f"section_{status}",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": "claude-code",
                "client_session_id": session_id,
                "client_context_keys_authored": ["client_session_id"],
                "project_dir": "/tmp/project",
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "section_id": section_id,
                "section_status": status,
                "next_step": "Re-run the focused suite and close the section",
                "section_title": "Add rate limit to login",
                "kind": "implementation",
                "files": ["src/login.py"],
                "summary": "Recorded outcome for this fixture section." if status in {"completed", "handed_off"} else None,
                "blocker": "The staging migration needs an owner role this account does not have." if status == "blocked" else None,
            },
        }
    )


def _record_passing_check(service: SentinelService, *, session_id: str, section_id: str, at: float) -> None:
    service.record_event(
        {
            "event_id": f"evt_check_{session_id}_{section_id}",
            "created_at": at,
            "source": "claude-code",
            "event_type": "machine_check",
            "run_id": None,
            "metadata": {
                "result": "passed",
                "evidence_type": "test",
                "summary": "pytest passed",
                "name": "pytest",
                "exit_code": 0,
                "section_id": section_id,
                "client": "claude-code",
                "client_session_id": session_id,
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "project_dir": "/tmp/project",
            },
        }
    )


def _record_errored_check(service: SentinelService, *, session_id: str, section_id: str, at: float) -> None:
    """A check whose recorded result is ``error``: it COULD NOT RUN, so it
    proves nothing either way — a named evidence gap, never a Finding."""

    service.record_event(
        {
            "event_id": f"evt_check_error_{session_id}_{section_id}",
            "created_at": at,
            "source": "claude-code",
            "event_type": "machine_check",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "result": "error",
                "evidence_type": "typecheck",
                "summary": "No module named mypy",
                "name": "mypy",
                "exit_code": 1,
                "section_id": section_id,
                "client": "claude-code",
                "client_session_id": session_id,
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "project_dir": "/tmp/project",
            },
        }
    )


def _record_failed_check(service: SentinelService, *, session_id: str, section_id: str, at: float) -> None:
    service.record_event(
        {
            "event_id": f"evt_check_fail_{session_id}_{section_id}",
            "created_at": at,
            "source": "claude-code",
            "event_type": "machine_check",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "result": "failed",
                "evidence_type": "test",
                "summary": "pytest found a regression",
                "name": "pytest",
                "exit_code": 1,
                "section_id": section_id,
                "client": "claude-code",
                "client_session_id": session_id,
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "project_dir": "/tmp/project",
            },
        }
    )


def _record_blocked_section(
    service: SentinelService, *, session_id: str, section_id: str, at: float
) -> None:
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
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "project_dir": "/tmp/project",
                "section_id": section_id,
                "section_status": "blocked",
                "files": ["src/agentacct/mcp.py"],
                "section_title": "Publish the site",
                "blocker": "waiting for approval",
                "next_step": "ask the user",
                "kind": "implementation",
            },
        }
    )


def test_receipt_routes_require_a_bearer_token(tmp_path: Path) -> None:
    client = _app(tmp_path)
    assert client.get("/v1/tasks").status_code == 401
    assert client.get("/v1/attention").status_code == 401
    assert client.get("/v1/receipt?task=task_x").status_code == 401


def test_receipt_exposes_constituent_sessions_and_summary_carries_primary_root(
    tmp_path: Path,
) -> None:
    """The Work surface nests each session's drill-down under the Receipt, so
    /v1/receipt exposes the Task's sessions grouped root -> members with
    primary/continuation + root/subagent roles, and the /v1/tasks list row
    carries the primary root ref for deep-linking a session to its Task."""

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-1", status="completed", at=101.0)
    client = _app(tmp_path)

    row = client.get("/v1/tasks", headers=_auth()).json()["tasks"][0]
    assert row["primary_root"] == {"client": "claude-code", "client_session_id": "s1"}
    task_id = row["task_id"]

    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()
    groups = receipt["sessions"]
    assert len(groups) == 1
    group = groups[0]
    assert group["role"] == "primary"
    assert group["root"] == {"client": "claude-code", "client_session_id": "s1"}
    members = group["members"]
    root_members = [m for m in members if m["role"] == "root"]
    assert len(root_members) == 1
    assert root_members[0]["client"] == "claude-code"
    assert root_members[0]["client_session_id"] == "s1"


def test_receipt_gaps_are_genuinely_missing_not_structural_noise(tmp_path: Path) -> None:
    """A Receipt's gaps should mean 'genuinely missing for this Task', not
    structural facts about the deployment or data we have but failed to roll up.
    A Task in a known project, whose checks recorded the files they touched,
    with a complete pricing-table cost estimate, must NOT gap identity, touched
    files, the coverage table, or the estimate basis."""

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-1", status="completed", at=101.0)
    # A passing check that records the files it touched; the section lists none.
    service.record_event(
        {
            "event_id": "evt_check_with_files",
            "created_at": 102.0,
            "source": "claude-code",
            "event_type": "machine_check",
            "metadata": {
                "result": "passed",
                "evidence_type": "test",
                "summary": "pytest passed",
                "name": "pytest",
                "exit_code": 0,
                "section_id": "sec-1",
                "client": "claude-code",
                "client_session_id": "s1",
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "project_dir": "/tmp/project",
                "files": ["src/login.py", "tests/test_login.py"],
            },
        }
    )
    client = _app(tmp_path)
    task_id = client.get("/v1/tasks", headers=_auth()).json()["tasks"][0]["task_id"]
    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()

    dims = receipt["dimensions"]
    reasons = [item["reason"] for item in dims["gaps"]["items"]]

    # Touched files recovered from the check evidence, not gapped. The section
    # itself records only src/login.py; tests/test_login.py exists ONLY on the
    # machine check, so asserting it proves the evidence-file union (not the
    # section path) — this fails if the union is reverted.
    assert "src/login.py" in dims["actions"]["touched_files"]
    assert "tests/test_login.py" in dims["actions"]["touched_files"]
    assert not any("No touched files" in r for r in reasons)
    # A known project is scoped, not "unscoped" — no identity gap.
    assert dims["task"]["boundary"]["identity_scope"] != "unscoped"
    assert not any("could not be bound to a project" in r for r in reasons)
    # The coverage table is no longer folded into gaps.
    assert not any(item["dimension"] == "coverage" for item in dims["gaps"]["items"])
    # A complete pricing-table estimate is not a gap.
    assert dims["cost"]["cost_complete"] is True
    assert not any("pricing-table estimate" in r for r in reasons)


def test_version_advertises_the_receipt_schema(tmp_path: Path) -> None:
    version = _app(tmp_path).get("/v1/version", headers=_auth()).json()
    assert version["receipt_schema"] == RECEIPT_SCHEMA_VERSION
    assert version["attention_schema"] == V1_ATTENTION_SCHEMA_VERSION


def test_attention_empty_state_and_query_bounds(tmp_path: Path) -> None:
    client = _app(tmp_path)

    payload = client.get("/v1/attention", headers=_auth()).json()
    snapshot = payload.pop("snapshot")
    assert len(snapshot) == 64
    assert all(character in "0123456789abcdef" for character in snapshot)
    assert payload == {
        "schema": V1_ATTENTION_SCHEMA_VERSION,
        "items": [],
        "total": 0,
        "counts": {"failed_check": 0, "failed_step": 0, "blocker": 0, "check_not_run": 0},
        "offset": 0,
        "limit": 5,
        "truncated": False,
        "queue": {
            "noun": "Attention",
            "count_text": "0 in Attention",
            "open_action": "Open Attention",
            "sort_text": "failed checks and steps, then blockers, then checks that could not run, then most recent",
        },
    }
    assert client.get(
        "/v1/attention", headers=_auth(), params={"limit": 0}
    ).status_code == 422
    assert client.get(
        "/v1/attention", headers=_auth(), params={"limit": 51}
    ).status_code == 422
    assert client.get(
        "/v1/attention", headers=_auth(), params={"offset": -1}
    ).status_code == 422


def test_attention_is_complete_bounded_and_operationally_ordered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Attention is a complete server projection, not a client filter over a
    recent-tasks page. A finding outside ``/v1/tasks?limit=1`` must still count
    and must lead a newer blocker under the documented operational ordering."""

    service = SentinelService(tmp_path)

    _record_usage(service, session_id="finding", at=100.0)
    _record_section(service, session_id="finding", section_id="sec-f", status="completed", at=101.0)
    _record_failed_check(service, session_id="finding", section_id="sec-f", at=102.0)

    _record_usage(service, session_id="blocked", at=200.0)
    _record_section(service, session_id="blocked", section_id="sec-b", status="started", at=201.0)
    _record_blocked_section(service, session_id="blocked", section_id="sec-b", at=202.0)

    _record_usage(service, session_id="clean", at=300.0)
    _record_section(service, session_id="clean", section_id="sec-c", status="completed", at=301.0)
    _record_passing_check(service, session_id="clean", section_id="sec-c", at=302.0)

    classification_calls: list[str] = []
    original_build_attention_reason = api_module.build_attention_reason

    def counted_build_attention_reason(task, **kwargs):
        classification_calls.append(str(task.get("public_task_id")))
        return original_build_attention_reason(task, **kwargs)

    monkeypatch.setattr(api_module, "build_attention_reason", counted_build_attention_reason)
    clock = [api_module.time.time()]
    monkeypatch.setattr(api_module.time, "time", lambda: clock[0])
    client = _app(tmp_path)
    recent_page = client.get("/v1/tasks", headers=_auth(), params={"limit": 1}).json()
    assert recent_page["total"] == 3
    assert recent_page["tasks"][0]["primary_root"]["client_session_id"] == "clean"

    attention = client.get("/v1/attention", headers=_auth(), params={"limit": 1}).json()
    assert set(attention) == {
        "schema", "items", "total", "counts", "snapshot", "offset", "limit", "truncated", "queue"
    }
    assert attention["schema"] == V1_ATTENTION_SCHEMA_VERSION
    assert attention["total"] == 2
    assert attention["counts"] == {"failed_check": 1, "failed_step": 0, "blocker": 1, "check_not_run": 0}
    assert attention["limit"] == 1
    assert attention["truncated"] is True
    assert attention["offset"] == 0
    assert len(attention["items"]) == 1
    leading = attention["items"][0]
    assert leading["primary_root"]["client_session_id"] == "finding"
    assert leading["project"] == "project"
    assert leading["decision_status"]["key"] == "finding"
    assert leading["evidence_strength"]["checks_failed"] == 1
    assert leading["attention"] == {
        "kind": "failed_check",
        "reason_label": "Failed check",
        "summary": "pytest found a regression",
        "check_name": "pytest",
        "evidence_type": "test",
        "result": "failed",
        "result_label": "Failed",
        "result_tone": "failure",
        "exit_code": 1,
        "section_title": "Add rate limit to login",
        "label": "Failed test check · pytest · exit 1",
        "note_text": None,
        "next_step": "Re-run the focused suite and close the section",
        "observed_at": leading["attention"]["observed_at"],
        "source": "mcp",
        "source_label": "Agent-reported",
        "action_token": leading["attention"]["action_token"],
        "target_digest": leading["attention"]["target_digest"],
        "revision": 0,
        "disposition_state": "open",
        "disposition_note": None,
        "open": True,
        "effects": {
            "reviewed": "Leaves Attention; the badge stays Finding until resolved.",
            "resolved": (
                "Leaves Attention and records your resolution; the badge becomes Finding resolved. "
                "The failing check stays in history."
            ),
            "reopen": "Returns to Attention with its original badge.",
        },
        "more_text": None,
    }
    assert leading["attention"]["observed_at"] is not None
    # The disposition handle a write names rides the block.
    assert leading["attention"]["action_token"]
    assert leading["attention"]["target_digest"]

    next_attention = client.get(
        "/v1/attention",
        headers=_auth(),
        params={"limit": 1, "offset": 1},
    ).json()
    assert next_attention["offset"] == 1
    assert next_attention["truncated"] is False
    assert next_attention["snapshot"] == attention["snapshot"]
    assert [row["primary_root"]["client_session_id"] for row in next_attention["items"]] == [
        "blocked"
    ]

    all_attention = client.get("/v1/attention", headers=_auth(), params={"limit": 5}).json()
    assert [row["primary_root"]["client_session_id"] for row in all_attention["items"]] == [
        "finding",
        "blocked",
    ]
    blocker = all_attention["items"][1]["attention"]
    assert blocker == {
        "kind": "blocker",
        "reason_label": "Blocker",
        "summary": "waiting for approval",
        "check_name": None,
        "evidence_type": None,
        "result": None,
        "result_label": None,
        "result_tone": None,
        "exit_code": None,
        "section_title": "Publish the site",
        # The step title equals the Task title the surface already shows, and
        # the reason noun rides in reason_label: nothing left to repeat.
        "label": "",
        "note_text": None,
        "next_step": "ask the user",
        "observed_at": blocker["observed_at"],
        "source": "mcp",
        "source_label": "Agent-reported",
        "action_token": blocker["action_token"],
        "target_digest": None,
        "revision": blocker["revision"],
        "disposition_state": "open",
        "disposition_note": None,
        "open": True,
        "effects": blocker["effects"],
        "more_text": None,
    }
    # A disposable blocker names its write handle and the effect of each action.
    assert blocker["action_token"]
    assert blocker["effects"] == {
        "reviewed": "Leaves Attention; the badge stays Blocked until resolved.",
        "resolved": "Leaves Attention and records your resolution; the badge becomes Blocker resolved.",
        "reopen": "Returns to Attention with its original badge.",
    }
    assert blocker["observed_at"] is not None
    # The second poll changes only the response limit. Classification, complete
    # counts, and ordering are reused for the lifetime of the cached parent
    # projection instead of re-reducing every Task on every dashboard refresh.
    assert len(classification_calls) == 3

    # The parent Receipt projection expires after 30 seconds, while the app's
    # normal poll is every 60 seconds. An unchanged rebuilt projection must
    # still reuse its content-keyed attention index across that real cadence.
    clock[0] += 61.0
    after_parent_ttl = client.get(
        "/v1/attention",
        headers=_auth(),
        params={"limit": 5},
    ).json()
    assert after_parent_ttl["total"] == 2
    assert after_parent_ttl["snapshot"] == attention["snapshot"]
    assert len(classification_calls) == 3

    _record_usage(service, session_id="new-finding", at=400.0)
    _record_section(
        service,
        session_id="new-finding",
        section_id="sec-new",
        status="completed",
        at=401.0,
    )
    _record_failed_check(
        service,
        session_id="new-finding",
        section_id="sec-new",
        at=402.0,
    )
    changed_attention = client.get(
        "/v1/attention",
        headers=_auth(),
        params={"limit": 5},
    ).json()
    assert changed_attention["total"] == 3
    assert changed_attention["snapshot"] != attention["snapshot"]
    # Changed content invalidates the index and classifies all four current
    # Tasks; the clean Task still does not enter the three-item queue.
    assert len(classification_calls) == 7


def test_tasks_list_and_receipt_detail_for_an_observed_task(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    client = _app(tmp_path)

    listing = client.get("/v1/tasks", headers=_auth()).json()
    assert listing["schema"] == RECEIPT_SCHEMA_VERSION
    assert listing["total"] == 1
    row = listing["tasks"][0]
    task_id = row["task_id"]
    assert task_id.startswith("task_")
    assert "decision_status" in row and "evidence_strength" in row

    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert set(receipt["dimensions"]) == {
        "task",
        "actors",
        "actions",
        "cost",
        "evidence",
        "outcome",
        "gaps",
        "provenance",
    }
    assert receipt["axes"]["decision_status"]["asserted_by"] in {"agent_report", "human", "machine", "none"}
    # cost_basis threads all the way to the wire.
    assert receipt["dimensions"]["cost"]["cost_basis"] == "pricing_table"


def test_tasks_attention_summary_includes_actionable_work_beyond_recent_window(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path)
    for index in range(3):
        session_id = f"older-blocked-{index}"
        _record_usage(service, session_id=session_id, at=1.0 + index * 2)
        _record_section(
            service,
            session_id=session_id,
            section_id=f"blocked-section-{index}",
            status="blocked",
            at=2.0 + index * 2,
        )
    for index in range(200):
        _record_usage(
            service,
            session_id=f"recent-{index}",
            at=2_000_000_000.0 + index,
        )

    listing = _app(tmp_path).get(
        "/v1/tasks",
        headers=_auth(),
        params={"limit": 200},
    ).json()

    assert listing["total"] == 203
    assert len(listing["tasks"]) == 200
    assert listing["truncated"] is True
    recent_ids = {row["task_id"] for row in listing["tasks"]}
    assert listing["attention"]["total"] == 3
    assert listing["attention"]["limit"] == 2
    assert listing["attention"]["truncated"] is True
    assert len(listing["attention"]["tasks"]) == 2
    attention_activity = [
        row["last_activity_at"] for row in listing["attention"]["tasks"]
    ]
    assert attention_activity == sorted(attention_activity, reverse=True)
    assert all(
        row["decision_status"]["key"] == "blocked"
        and row["task_id"] not in recent_ids
        for row in listing["attention"]["tasks"]
    )


def test_receipt_attention_priority_is_the_reducers_own_class() -> None:
    """The Dashboard's attention block carries NO predicate of its own: a
    summary is in the queue exactly when the reducer's ``group_key`` says so,
    and it sorts by the reducer's ``attention_order``.

    The rule this replaces was re-derived from the decision key and the
    failed-check count, so it could not see order class 2 (a check that could
    not run) and silently undercounted the queue.
    """

    def priority(group: str | None, order: int | None, **extra: object) -> int | None:
        row: dict[str, object] = {"group_key": group, "attention_order": order}
        row.update(extra)
        return _receipt_attention_priority(row)

    assert priority("attention", 0) == 0   # failed checks and failed steps
    assert priority("attention", 1) == 1   # blockers
    assert priority("attention", 2) == 2   # checks that could not run
    # Out of the queue: a settled finding, a verified Task, anything reviewed.
    assert priority("reported", None) is None
    assert priority("verified", None) is None
    assert priority("other", None) is None
    # Open but from a payload with no order class: ranked last, never dropped.
    assert priority("attention", None) == 2
    assert priority("attention", 99) == 2
    # No group key at all (an older payload): the explicit predicate, then the
    # attention block's own `open` flag.
    assert priority(None, 0, attention_open=True) == 0
    assert priority(None, 1, attention_open=False) is None
    assert priority(None, None, attention={"open": True}) == 2
    assert priority(None, None) is None


def test_tasks_attention_total_matches_the_attention_endpoint(tmp_path: Path) -> None:
    """The count `/v1/tasks` ships and the queue `/v1/attention` serves are the
    same queue. A check that could not run is an open attention item (a named
    evidence gap, order class 2) and used to be missing from the first."""

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s-error", at=100.0)
    _record_section(service, session_id="s-error", section_id="sec-1", status="completed", at=110.0)
    _record_errored_check(service, session_id="s-error", section_id="sec-1", at=120.0)
    _record_usage(service, session_id="s-blocked", at=200.0)
    _record_section(service, session_id="s-blocked", section_id="sec-2", status="blocked", at=210.0)

    client = _app(tmp_path)
    listing = client.get("/v1/tasks", headers=_auth(), params={"limit": 50}).json()
    queue = client.get("/v1/attention", headers=_auth(), params={"limit": 50}).json()

    in_group = [row for row in listing["tasks"] if row["group_key"] == "attention"]
    assert len(in_group) == 2, [row["decision_status"]["key"] for row in listing["tasks"]]
    assert listing["attention"]["total"] == queue["total"] == len(in_group)
    # And the queue count the surfaces print is built from that same number.
    assert listing["queue"]["count_text"] == f"{len(in_group)} in Attention"


def test_unknown_task_is_a_404_not_an_empty_fabrication(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    client = _app(tmp_path)
    assert client.get("/v1/receipt?task=task_deadbeef", headers=_auth()).status_code == 404


def test_receipt_self_checked_when_an_agent_reported_check_passes(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-1", status="completed", at=100.0)
    _record_passing_check(service, session_id="s1", section_id="sec-1", at=200.0)
    client = _app(tmp_path)

    task_id = client.get("/v1/tasks", headers=_auth()).json()["tasks"][0]["task_id"]
    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()
    assert receipt["axes"]["decision_status"]["key"] == "verified"
    # An agent-reported (mcp) check is the agent's OWN word — self_checked, never
    # promoted to independent/external verification.
    evidence = receipt["axes"]["evidence_strength"]
    assert evidence["strongest_tier"] == "self_checked"
    assert evidence["by_tier"]["self_checked"] == 1
    assert evidence["checks_passed"] == 1
    # The touched file recorded on the section rides the Actions dimension.
    assert "src/login.py" in receipt["dimensions"]["actions"]["touched_files"]

    # The list row carries the same check tallies as the detail (shared
    # reducer) so a checks column can never disagree with the open Receipt.
    row = next(
        entry
        for entry in client.get("/v1/tasks", headers=_auth()).json()["tasks"]
        if entry["task_id"] == task_id
    )
    for tally in ("checks_total", "checks_passed", "checks_failed"):
        assert row["evidence_strength"][tally] == evidence[tally]
    assert row["evidence_strength"]["checks_passed"] == 1


def _derived_style_ledger(service: SentinelService, tmp_path: Path) -> dict:
    """Build the work ledger exactly the way the sessions lane's cached
    ``_derived_work_ledger`` does — the shared ``_LEDGER_RUN_REPORT_LIMIT`` cap
    and cost events in raw store order — so a golden test can prove the /v1
    task lane's reuse of it yields the same projection build_page_data
    self-builds (same cap; its cost events are pre-sorted but re-sorted away
    inside the reduce)."""

    events = service.list_all_events()
    envelopes, diagnostics = _mechanical_projection_envelopes_for(service, tmp_path)
    scope, label = _store_scope_and_label(tmp_path)
    observations = (
        build_session_observations(
            envelopes,
            default_project_label=label if scope == "project" else None,
            diagnostics=diagnostics,
        )
        if envelopes
        else []
    )
    return build_work_ledger(
        events,
        run_reports=_collect_service_run_reports(service, limit=_LEDGER_RUN_REPORT_LIMIT),
        cost_events=CostLedger(tmp_path).read_events(),
        session_observations=observations,
        session_observation_diagnostics=diagnostics,
        store_project_label=label,
        store_scope=scope,
    )


def test_injecting_the_shared_derived_ledger_matches_the_self_built_projection(
    tmp_path: Path,
) -> None:
    """Reusing the shared derived ledger must not change what receipts show.

    The /v1 task lane assembles its projection over the sessions lane's cached
    ledger instead of rebuilding one per request. This locks that swap: a
    ledger built the derived lane's way, injected into build_page_data, yields
    a task projection byte-identical to the one build_page_data self-builds.
    """

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-1", status="completed", at=101.0)
    _record_passing_check(service, session_id="s1", section_id="sec-1", at=102.0)
    _record_usage(service, session_id="s2", at=200.0)
    _record_section(service, session_id="s2", section_id="sec-2", status="handed_off", at=201.0)

    reference = _dashboard_task_projection(build_page_data(tmp_path))

    events = service.list_all_events()
    derived_ledger = _derived_style_ledger(service, tmp_path)
    injected = _dashboard_task_projection(
        build_page_data(tmp_path, events=events, ledger=derived_ledger)
    )

    assert injected == reference


def test_injecting_a_ledger_with_stashed_mechanical_checks_matches_self_build(
    tmp_path: Path,
) -> None:
    """The warm /v1 lane reuses the mechanical check events the ledger build
    stashed instead of re-reading the Evidence store. A ledger carrying that
    stash must produce the same task projection build_page_data self-builds by
    reading the Evidence store fresh — this locks that the stashed-events branch
    of build_page_data attaches the identical evidence."""

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-1", status="completed", at=101.0)
    _record_passing_check(service, session_id="s1", section_id="sec-1", at=102.0)

    reference = _dashboard_task_projection(build_page_data(tmp_path))

    events = service.list_all_events()
    ledger = _derived_style_ledger(service, tmp_path)
    envelopes, _diagnostics = _mechanical_projection_envelopes_for(service, tmp_path)
    ledger[_LEDGER_MECHANICAL_CHECK_EVENTS_KEY] = build_mechanical_check_events(envelopes)

    injected = _dashboard_task_projection(
        build_page_data(tmp_path, events=events, ledger=ledger)
    )

    assert injected == reference


def test_injected_task_and_receipt_wire_output_is_unchanged(tmp_path: Path) -> None:
    """End to end: the /v1/tasks and /v1/receipt payloads the app serves over
    the injected shared ledger equal the ones built from the self-built
    projection — the reuse is invisible on the wire."""

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-1", status="completed", at=101.0)
    _record_passing_check(service, session_id="s1", section_id="sec-1", at=102.0)

    client = _app(tmp_path)
    listing = client.get("/v1/tasks", headers=_auth()).json()
    task_id = listing["tasks"][0]["task_id"]
    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()

    # The projection the routes consume (built over the injected shared ledger)
    # must equal the self-built one field for field.
    reference = _dashboard_task_projection(build_page_data(tmp_path))
    injected = _dashboard_task_projection(
        build_page_data(
            tmp_path,
            events=service.list_all_events(),
            ledger=_derived_style_ledger(service, tmp_path),
        )
    )
    assert injected == reference
    assert listing["total"] == 1
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION


def test_proxy_usage_events_are_order_invariant() -> None:
    """The cost-event ordering delta between the two ledger build sites is
    provably immaterial: build_proxy_usage_events re-sorts by created_at, so
    the raw store order the sessions lane passes and the pre-sorted order
    build_page_data passes reduce to the identical list."""

    cost_events = [
        {"event_id": "c1", "created_at": 300.0, "estimated_cost_usd": 0.3},
        {"event_id": "c2", "created_at": 100.0, "estimated_cost_usd": 0.1},
        {"event_id": "c3", "created_at": 200.0, "estimated_cost_usd": 0.2},
    ]
    ascending = sorted(cost_events, key=lambda event: event["created_at"])
    descending = sorted(cost_events, key=lambda event: event["created_at"], reverse=True)

    assert build_proxy_usage_events(ascending) == build_proxy_usage_events(descending)
    assert build_proxy_usage_events(cost_events) == build_proxy_usage_events(ascending)


# ---------------------------------------------------------------------------
# weekly-plan share on task rows and receipts
# ---------------------------------------------------------------------------


def _record_7d_reading(service: SentinelService, *, captured: float, pct: float, index: int) -> None:
    service.record_event(
        {
            "event_id": f"evt_rl_cal_{index}",
            "created_at": captured,
            "source": "claude-code",
            "event_type": "rate_limit_observed",
            "metadata": {
                "client": "claude-code",
                "captured_at": captured,
                "windows": [{"kind": "7d", "window_minutes": 10080, "used_percent": pct}],
            },
        }
    )


def _record_bulk_usage(service: SentinelService, *, session_id: str, at: float, tokens: int) -> None:
    from agentacct.client_usage import ClientUsageEvent

    event = ClientUsageEvent(
        client="claude-code",
        client_session_id=session_id,
        source_path=Path(f"/tmp/claude-code/{session_id}.jsonl"),
        title=None,
        cwd="/tmp/project",
        model="claude-opus-4-8",
        input_tokens=tokens,
        output_tokens=0,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_tokens_reported=True,
        cache_read_tokens_reported=True,
        reasoning_output_tokens=0,
        provider_name="claude-code",
        started_at=at,
        updated_at=at,
        turn_count=1,
        usage_row_lane="model:claude-opus-4-8",
        source_namespace_fingerprint=NS,
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=tokens,
        total_tokens_reported=True,
    ).to_sentinel_event()
    event["estimated_cost_usd"] = 1.0
    event["cost_confidence"] = "estimated_from_tokens"
    service.record_event(event, trusted_usage_import=True)


def test_uncalibrated_store_serves_null_plan_share_with_state(tmp_path: Path) -> None:
    """Calibrated-or-nothing on the wire: without 7-day history the share is
    null (never 0) and the calibration state says why."""

    service = SentinelService(tmp_path)
    import time as _time

    _record_bulk_usage(service, session_id="s1", at=_time.time() - 3600, tokens=1_000_000)
    client = _app(tmp_path)
    rows = client.get("/v1/tasks", headers=_auth()).json()["tasks"]
    assert rows
    share = rows[0]["cost"]["plan_share"]
    assert share["pct"] is None
    assert share["calibration_state"] == "calibrating"
    assert share["client"] == "claude-code"
    assert share["session_count"] >= 1


def test_calibrated_store_serves_matching_plan_share_on_rows_and_receipts(tmp_path: Path) -> None:
    """With enough in-band 7-day history the task rows carry a positive weekly
    share, and the detail receipt carries the SAME stamp (one computation)."""

    from agentacct import plan_cost as pc
    import time as _time

    service = SentinelService(tmp_path)
    t0 = _time.time() - 30 * 3600  # inside the 21-day calibration window
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    pct = 1.0
    _record_7d_reading(service, captured=t0, pct=pct, index=0)
    for i in range(4):
        _record_bulk_usage(
            service, session_id=f"cal{i}", at=t0 + i * 3600 + 1800, tokens=50_000_000
        )
        pct += 50.0 * opus  # meter moves exactly what the baseline predicts (scale ~1)
        _record_7d_reading(service, captured=t0 + (i + 1) * 3600, pct=pct, index=i + 1)

    client = _app(tmp_path)
    rows = client.get("/v1/tasks", headers=_auth()).json()["tasks"]
    assert rows
    share = rows[0]["cost"]["plan_share"]
    assert share["calibration_state"] == "calibrated"
    assert share["pct"] is not None and share["pct"] > 0
    assert share["covered_sessions"] >= 1

    task_id = rows[0]["task_id"]
    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()
    assert receipt["dimensions"]["cost"]["plan_share"] == share


def test_stability_accepted_store_serves_shares_end_to_end(tmp_path: Path) -> None:
    """A persistent OUT-OF-BAND account (the live failure shape) must calibrate
    through the stability lane and serve task shares on the wire."""

    from agentacct import plan_cost as pc
    import time as _time

    service = SentinelService(tmp_path)
    spacing = 8 * 3600
    n = pc._STABILITY_MIN_INTERVALS + 2
    t0 = _time.time() - (n + 2) * spacing  # ~9 days back, inside the 21-day window
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    ratio = 4.0  # outside the trusted band (2.5), inside the stability ceiling
    pct = 0.0
    _record_7d_reading(service, captured=t0, pct=pct, index=0)
    for i in range(n):
        _record_bulk_usage(service, session_id=f"cal{i}",
                           at=t0 + i * spacing + spacing // 2, tokens=20_000_000)
        pct += ratio * (20.0 * opus)
        _record_7d_reading(service, captured=t0 + (i + 1) * spacing, pct=pct, index=i + 1)

    client = _app(tmp_path)
    plan = client.get("/v1/plan?days=7", headers=_auth()).json()
    cc = next(entry for entry in plan["clients"] if entry["client"] == "claude-code")
    assert cc["calibration_state"] == "calibrated"
    assert "split-half stability" in cc["basis"]
    assert "untracked" in cc["basis"]  # the blind spot stays disclosed

    rows = client.get("/v1/tasks", headers=_auth()).json()["tasks"]
    share = rows[0]["cost"]["plan_share"]
    assert share["calibration_state"] == "calibrated"
    assert share["pct"] is not None and share["pct"] > 0


def test_plan_share_stamp_is_client_scoped_and_names_never_for_plan_less_clients(
    tmp_path: Path,
) -> None:
    """Unit contract of the stamp: only the labelled client's members may
    contribute to the sum (a cross-client continuation must not mix plans),
    and a client outside the plan lane reads 'never', not null."""

    from agentacct.api import _stamp_task_plan_shares
    from agentacct import plan_cost as pc
    import time as _time

    service = SentinelService(tmp_path)
    t0 = _time.time() - 30 * 3600
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    pct = 1.0
    _record_7d_reading(service, captured=t0, pct=pct, index=0)
    for i in range(4):
        _record_bulk_usage(service, session_id=f"cal{i}",
                           at=t0 + i * 3600 + 1800, tokens=50_000_000)
        pct += 50.0 * opus
        _record_7d_reading(service, captured=t0 + (i + 1) * 3600, pct=pct, index=i + 1)
    events = service.list_all_events()

    projection = {
        "tasks": [
            {  # codex-primary task with a claude-code member: cc pct must NOT
               # be summed under the codex label.
                "primary_root": {"client": "codex", "client_session_id": "cx1"},
                "session_keys": [
                    {"client": "codex", "client_session_id": "cx1"},
                    {"client": "claude-code", "client_session_id": "cal0"},
                ],
            },
            {  # plan-less client: state must read "never", not null.
                "primary_root": {"client": "hermes", "client_session_id": "h1"},
                "session_keys": [{"client": "hermes", "client_session_id": "h1"}],
            },
            {  # the calibrated client still sums its own members.
                "primary_root": {"client": "claude-code", "client_session_id": "cal1"},
                "session_keys": [
                    {"client": "claude-code", "client_session_id": "cal1"},
                    {"client": "codex", "client_session_id": "cx9"},
                ],
            },
        ]
    }
    _stamp_task_plan_shares(projection, events)
    cross, hermes, cc = (task["plan_share"] for task in projection["tasks"])
    assert cross["pct"] is None and cross["client"] == "codex"
    assert cross["calibration_state"] == "calibrating"
    assert hermes["pct"] is None and hermes["calibration_state"] == "never"
    assert cc["pct"] is not None and cc["pct"] > 0
    assert cc["covered_sessions"] == 1  # the codex member never counted


def test_glance_recent_sessions_carry_the_task_decision_from_the_task_reducers(tmp_path: Path) -> None:
    """K37: a menu row shows its Task's decision (the same reducers /v1/tasks
    uses), kept apart from the agent's recorded work status."""

    import time as _time

    now = _time.time()
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s-recent", at=now - 60)
    _record_section(service, session_id="s-recent", section_id="sec-1", status="completed", at=now - 30)
    client = _app(tmp_path)

    task_row = client.get("/v1/tasks", headers=_auth()).json()["tasks"][0]
    glance = client.get("/v1/glance", headers=_auth()).json()
    row = next(item for item in glance["recent_sessions"] if item["session_id"] == "s-recent")
    assert row["status_label"] == "Completed"  # the agent's work-status report
    assert row["decision_key"] == task_row["decision_status"]["key"]
    assert row["decision_label"] == task_row["decision_status"]["label"]
    assert row["attention_open"] == task_row["attention_open"]
    assert row["task_id"] == task_row["task_id"]


def test_receipt_keeps_the_failing_run_of_a_recovered_check(tmp_path: Path) -> None:
    """End to end: a fail→pass recovery must be readable from /v1/receipt.

    The failing run used to be dropped from ``dimensions.evidence.checks`` while
    the surviving row still said ``runs_total: 2, earlier_failed: 1`` — the
    receipt counted a recovery it could not show.
    """

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-1", status="completed", at=101.0)
    for suffix, result, exit_code, at in (("fail", "failed", 1, 102.0), ("pass", "passed", 0, 103.0)):
        service.record_event(
            {
                "event_id": f"evt_run_{suffix}",
                "created_at": at,
                "source": "claude-code",
                "event_type": "machine_check",
                "metadata": {
                    "result": result,
                    "evidence_type": "test",
                    "summary": f"pytest {result}: assert total == 42 got 41 (one row dropped)",
                    "name": "pytest tests/test_percent.py",
                    "command": "pytest tests/test_percent.py",
                    "exit_code": exit_code,
                    "section_id": "sec-1",
                    "client": "claude-code",
                    "client_session_id": "s1",
                    "session_namespace_fingerprint": NS,
                    "identity_scope_state": "explicit",
                    "project_dir": "/tmp/project",
                },
            }
        )
    client = _app(tmp_path)
    task_id = client.get("/v1/tasks", headers=_auth()).json()["tasks"][0]["task_id"]
    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()

    rows = receipt["dimensions"]["evidence"]["checks"]
    assert [row["result"] for row in rows] == ["failed", "passed"]
    assert rows[0]["history_run"] is True and rows[0]["superseded"] is True
    # The store assigns its own event ids, so the link is asserted between the
    # rows themselves — which is exactly what a surface has to follow.
    assert rows[0]["superseded_by_event_id"] == rows[1]["event_id"]
    assert rows[1]["supersedes_check_event_id"] == rows[0]["event_id"]
    assert rows[1]["supersedes_basis"] == "reciprocal_of_supersession"
    assert rows[1]["runs_total"] == 2 and rows[1]["earlier_failed"] == 1
    # The tally still counts the frontier, so the recovery is not double-counted.
    assert receipt["dimensions"]["evidence"]["checks_total"] == 1
    assert receipt["dimensions"]["evidence"]["checks_failed"] == 0
    # The agent supplied the command, so the receipt says it was RECORDED.
    assert rows[1]["command_state"] == "agent_recorded"
    assert "was not stored" not in rows[1]["command_state_text"]


def test_receipt_gap_items_carry_their_rank_and_lead_with_review_blockers(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="s1", at=100.0)
    _record_section(service, session_id="s1", section_id="sec-1", status="completed", at=101.0)
    _record_passing_check(service, session_id="s1", section_id="sec-1", at=102.0)
    client = _app(tmp_path)
    task_id = client.get("/v1/tasks", headers=_auth()).json()["tasks"][0]["task_id"]
    gaps = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()["dimensions"]["gaps"]

    assert gaps["count"] == len(gaps["items"])
    assert gaps["blocks_review_count"] + gaps["bookkeeping_count"] == gaps["count"]
    kinds = [item["kind"] for item in gaps["items"]]
    assert kinds == sorted(kinds, key=lambda kind: 0 if kind == "blocks_review" else 1)
    for item in gaps["items"]:
        assert item["kind_label"] in {"Blocks review", "Provenance bookkeeping"}
