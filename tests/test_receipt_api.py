"""The /v1/receipt and /v1/tasks native-shell lane."""

from __future__ import annotations

from pathlib import Path

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
from agentacct.receipt import RECEIPT_SCHEMA_VERSION
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


def test_receipt_routes_require_a_bearer_token(tmp_path: Path) -> None:
    client = _app(tmp_path)
    assert client.get("/v1/tasks").status_code == 401
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
