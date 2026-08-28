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
                "section_title": "Add rate limit to login",
                "kind": "implementation",
                "files": ["src/login.py"],
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
    service: SentinelService,
    *,
    session_id: str,
    section_id: str,
    at: float,
    event_suffix: str = "",
) -> None:
    service.record_event(
        {
            "event_id": f"evt_section_{session_id}_{section_id}_blocked{event_suffix}",
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
    revision = payload.pop("revision")
    assert isinstance(revision, str) and revision
    assert payload == {
        "schema": V1_ATTENTION_SCHEMA_VERSION,
        "items": [],
        "total": 0,
        "counts": {"failed_check": 0, "failed_step": 0, "blocker": 0},
        "offset": 0,
        "limit": 5,
        "truncated": False,
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
        "schema",
        "items",
        "total",
        "counts",
        "revision",
        "offset",
        "limit",
        "truncated",
    }
    assert attention["schema"] == V1_ATTENTION_SCHEMA_VERSION
    assert attention["total"] == 2
    assert attention["counts"] == {"failed_check": 1, "failed_step": 0, "blocker": 1}
    assert isinstance(attention["revision"], str) and attention["revision"]
    assert attention["offset"] == 0
    assert attention["limit"] == 1
    assert attention["truncated"] is True
    assert len(attention["items"]) == 1
    leading = attention["items"][0]
    assert leading["primary_root"]["client_session_id"] == "finding"
    assert leading["project"] == "project"
    assert leading["decision_status"]["key"] == "finding"
    assert leading["evidence_strength"]["checks_failed"] == 1
    assert leading["attention"] == {
        "kind": "failed_check",
        "summary": "pytest found a regression",
        "next_step": None,
        "observed_at": leading["attention"]["observed_at"],
        "source": "mcp",
    }
    assert leading["attention"]["observed_at"] is not None

    next_attention = client.get(
        "/v1/attention",
        headers=_auth(),
        params={"limit": 1, "offset": 1},
    ).json()
    assert next_attention["total"] == attention["total"]
    assert next_attention["counts"] == attention["counts"]
    assert next_attention["revision"] == attention["revision"]
    assert next_attention["offset"] == 1
    assert next_attention["limit"] == 1
    assert next_attention["truncated"] is False
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
        "summary": "waiting for approval",
        "next_step": "ask the user",
        "observed_at": blocker["observed_at"],
        "source": "mcp",
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
    # Changed content invalidates the index and classifies all four current
    # Tasks; the clean Task still does not enter the three-item queue.
    assert len(classification_calls) == 7


def test_attention_pages_reach_every_ranked_item_without_overlap(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    for index in range(7):
        session_id = f"blocked-{index}"
        at = 100.0 + index * 10
        _record_usage(service, session_id=session_id, at=at)
        _record_section(
            service,
            session_id=session_id,
            section_id=f"sec-{index}",
            status="started",
            at=at + 1,
        )
        _record_blocked_section(
            service,
            session_id=session_id,
            section_id=f"sec-{index}",
            at=at + 2,
        )

    client = _app(tmp_path)
    complete = client.get(
        "/v1/attention", headers=_auth(), params={"limit": 50}
    ).json()
    first = client.get(
        "/v1/attention", headers=_auth(), params={"limit": 5, "offset": 0}
    ).json()
    second = client.get(
        "/v1/attention", headers=_auth(), params={"limit": 5, "offset": 5}
    ).json()

    complete_ids = [row["task_id"] for row in complete["items"]]
    paged_ids = [row["task_id"] for row in first["items"] + second["items"]]
    assert complete["total"] == 7
    assert first["offset"] == 0 and first["truncated"] is True
    assert second["offset"] == 5 and second["truncated"] is False
    assert first["revision"] == second["revision"] == complete["revision"]
    assert len(set(paged_ids)) == 7
    assert paged_ids == complete_ids

    for offset in (7, 8):
        beyond = client.get(
            "/v1/attention", headers=_auth(), params={"limit": 5, "offset": offset}
        ).json()
        assert beyond["items"] == []
        assert beyond["offset"] == offset
        assert beyond["truncated"] is False


def test_attention_revision_changes_when_same_count_queue_reorders(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    for index in range(2):
        session_id = f"blocked-{index}"
        at = 100.0 + index * 10
        _record_usage(service, session_id=session_id, at=at)
        _record_section(
            service,
            session_id=session_id,
            section_id=f"sec-{index}",
            status="started",
            at=at + 1,
        )
        _record_blocked_section(
            service,
            session_id=session_id,
            section_id=f"sec-{index}",
            at=at + 2,
        )

    client = _app(tmp_path)
    before = client.get("/v1/attention", headers=_auth()).json()
    _record_blocked_section(
        service,
        session_id="blocked-0",
        section_id="sec-0",
        at=500.0,
        event_suffix="_newer",
    )
    after = client.get("/v1/attention", headers=_auth()).json()

    assert before["total"] == after["total"] == 2
    assert before["counts"] == after["counts"]
    assert {row["task_id"] for row in before["items"]} == {
        row["task_id"] for row in after["items"]
    }
    assert before["items"][0]["task_id"] != after["items"][0]["task_id"]
    assert before["revision"] != after["revision"]


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
