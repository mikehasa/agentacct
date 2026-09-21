"""Folder-anchored Work groupings ("worksets").

A workset is the local dashboard user's own overlay: the sessions under one
folder are one piece of work. It is append-only, server-validated, cross-source
(a Claude Code session and a Codex session in the same repo group together),
and it NEVER rewrites a session's Task identity, receipt, or evidence — the
membership is a labeled sum, never a re-graded verdict.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agentacct.service import SentinelService
from agentacct.work_ledger import _project_identity
from agentacct.worksets import (
    WORKSET_EVENT_TYPE,
    WorksetConflict,
    WorksetError,
    WorksetNotFound,
    folder_label_of,
    mark_trusted_workset,
    reduce_worksets,
    summarize_members,
    workset_candidates,
    workset_member_entries,
    workset_operation_digest,
    workset_session_lane,
)

from tests.test_receipt_api import TOKEN, _app, _auth


# --- pure model / reducer -----------------------------------------------------


def _trusted_event(*, revision: int, at: float, event_id: str, **metadata) -> dict:
    digest = workset_operation_digest(
        workset_id=metadata["workset_id"],
        action=metadata["action"],
        name=metadata.get("name"),
        project_identity=metadata.get("project_identity"),
        expected_revision=metadata["expected_revision"],
    )
    event = mark_trusted_workset(
        {
            "metadata": {
                **metadata,
                "revision": revision,
                "operation_digest": digest,
            }
        }
    )
    event["event_id"] = event_id
    event["created_at"] = at
    return event


def _create(idem="k-create", **over) -> dict:
    base = dict(
        workset_id="ws_1",
        action="create",
        name="tryairis.ai",
        project_identity="project:tryairis.ai:deadbeef",
        expected_revision=0,
        idempotency_key=idem,
    )
    base.update(over)
    return _trusted_event(revision=base["expected_revision"] + 1, at=1000.0, event_id="evt_create", **base)


def test_create_then_rename_then_delete_replays_in_order() -> None:
    create = _create()
    rename = _trusted_event(
        revision=2, at=1001.0, event_id="evt_rename",
        workset_id="ws_1", action="rename", name="airis site",
        expected_revision=1, idempotency_key="k-rename",
    )
    delete = _trusted_event(
        revision=3, at=1002.0, event_id="evt_delete",
        workset_id="ws_1", action="delete",
        expected_revision=2, idempotency_key="k-delete",
    )
    proj = reduce_worksets([create, rename, delete])
    state = proj.states["ws_1"]
    assert state.name == "airis site"
    assert state.project_identity == "project:tryairis.ai:deadbeef"
    assert state.deleted is True
    assert state.revision == 3
    assert proj.active() == []  # deleted worksets are not live


def test_redirect_changes_folder_keeps_name() -> None:
    create = _create()
    redirect = _trusted_event(
        revision=2, at=1001.0, event_id="evt_redirect",
        workset_id="ws_1", action="redirect",
        project_identity="project:other:cafef00d",
        expected_revision=1, idempotency_key="k-redirect",
    )
    state = reduce_worksets([create, redirect]).states["ws_1"]
    assert state.name == "tryairis.ai"
    assert state.project_identity == "project:other:cafef00d"


def test_stale_revision_is_rejected_not_applied() -> None:
    create = _create()
    # A second create-shaped event at the same expected_revision loses.
    stale = _trusted_event(
        revision=2, at=1001.0, event_id="evt_stale",
        workset_id="ws_1", action="rename", name="stale name",
        expected_revision=0, idempotency_key="k-stale",
    )
    proj = reduce_worksets([create, stale])
    assert proj.states["ws_1"].name == "tryairis.ai"
    assert "ws_1" in proj.invalid
    assert proj.states["ws_1"].chain_valid is False


def test_idempotent_duplicate_is_absorbed_not_double_applied() -> None:
    create = _create(idem="same-key")
    dup = _create(idem="same-key")
    dup["event_id"] = "evt_create_dup"
    proj = reduce_worksets([create, dup])
    assert proj.states["ws_1"].revision == 1
    assert proj.diagnostics["rejected_by_reason"].get("idempotent_duplicate") == 1
    assert "ws_1" not in proj.invalid


def test_forged_event_without_trusted_stamp_is_ignored() -> None:
    forged = {
        "event_type": WORKSET_EVENT_TYPE,
        "source": "some-agent",  # not the server-owned source
        "event_id": "evt_forged",
        "created_at": 1000.0,
        "metadata": {
            "workset_id": "ws_evil",
            "action": "create",
            "name": "evil",
            "project_identity": "project:evil:0000",
            "expected_revision": 0,
            "revision": 1,
            "idempotency_key": "k",
            "operation_digest": "0" * 64,
        },
    }
    proj = reduce_worksets([forged])
    assert proj.states == {}


def test_tampered_operation_digest_is_rejected() -> None:
    create = _create()
    create["metadata"]["operation_digest"] = "f" * 64
    proj = reduce_worksets([create])
    assert proj.states == {}  # no valid transition committed
    assert proj.diagnostics["rejected_by_reason"].get("operation_digest_mismatch") == 1


def test_mutating_absent_workset_never_creates_it() -> None:
    rename = _trusted_event(
        revision=1, at=1000.0, event_id="evt_rename_absent",
        workset_id="ws_absent", action="rename", name="x",
        expected_revision=0, idempotency_key="k",
    )
    proj = reduce_worksets([rename])
    assert "ws_absent" not in proj.states or proj.states["ws_absent"].chain_valid is False


def test_candidates_group_cross_source_roots_only() -> None:
    rollup = {"sessions": [
        {"session_key": "claude-code::s1", "client": "claude-code", "session_kind": "root",
         "project": "webapp", "project_identity": "project:webapp:aaaa",
         "project_identity_state": "explicit", "first_activity_at": 1.0, "last_activity_at": 2.0,
         "usage": {}},
        {"session_key": "codex::s2", "client": "codex", "session_kind": "root",
         "project": "webapp", "project_identity": "project:webapp:aaaa",
         "project_identity_state": "explicit", "first_activity_at": 3.0, "last_activity_at": 4.0,
         "usage": {}},
        {"session_key": "claude-code::child", "client": "claude-code", "session_kind": "child",
         "project": "webapp", "project_identity": "project:webapp:aaaa",
         "project_identity_state": "explicit", "usage": {}},
        {"session_key": "claude-code::wander", "client": "claude-code", "session_kind": "root",
         "project": "webapp", "project_identity": "project:webapp:aaaa",
         "project_identity_state": "conflicting", "usage": {}},
    ]}
    candidates = workset_candidates(rollup)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["session_count"] == 2  # child + conflicting excluded
    assert cand["sources"] == ["claude-code", "codex"]


def _wander_entry() -> dict:
    """A root session that ran across two folders in one run (``conflicting``).

    It has no single home ``project_identity``; ``project_identities`` records
    every folder it touched so a folder grouping can surface it under each.
    """

    return {
        "session_key": "claude-code::wander", "client": "claude-code", "session_kind": "root",
        "project": "webapp", "project_identity": None, "project_identity_state": "conflicting",
        "project_identities": ["project:api:bbbb", "project:webapp:aaaa"],
        "first_activity_at": 5.0, "last_activity_at": 6.0,
        "usage": {"total_tokens": 10, "estimated_cost_usd": 2.0, "cost_confidence": "estimated_from_tokens"},
    }


def test_folder_label_of_parses_leaf_or_returns_none() -> None:
    assert folder_label_of("project:webapp:aaaa") == "webapp"
    assert folder_label_of("project:agentacct-site:3bbbf554cc642299") == "agentacct-site"
    assert folder_label_of("not-an-identity") is None
    assert folder_label_of("project:missing-digest") is None
    assert folder_label_of(None) is None


def test_multi_folder_session_joins_every_folder_it_touched() -> None:
    rollup = {"sessions": [
        {"session_key": "claude-code::home", "client": "claude-code", "session_kind": "root",
         "project": "webapp", "project_identity": "project:webapp:aaaa",
         "project_identity_state": "explicit", "usage": {}},
        _wander_entry(),
    ]}
    webapp = workset_member_entries(rollup, "project:webapp:aaaa")
    api = workset_member_entries(rollup, "project:api:bbbb")
    assert {e["session_key"] for e in webapp} == {"claude-code::home", "claude-code::wander"}
    assert {e["session_key"] for e in api} == {"claude-code::wander"}  # shared run reaches its other folder


def test_shared_lane_is_flagged_with_its_home_set() -> None:
    lane = workset_session_lane(_wander_entry())
    assert lane["also_ran_elsewhere"] is True
    assert lane["home_identities"] == ["project:api:bbbb", "project:webapp:aaaa"]
    # A single-folder session is not flagged.
    solo = workset_session_lane(
        {"session_key": "claude-code::solo", "client": "claude-code", "session_kind": "root",
         "project": "webapp", "project_identity": "project:webapp:aaaa",
         "project_identity_state": "explicit", "usage": {}}
    )
    assert solo["also_ran_elsewhere"] is False
    assert solo["home_identities"] == ["project:webapp:aaaa"]


def test_member_lanes_disclose_the_other_folders_relative_to_this_group() -> None:
    from agentacct.api import _workset_member_lanes

    rollup = {"sessions": [_wander_entry()]}
    webapp = _workset_member_lanes(rollup, "project:webapp:aaaa")
    assert webapp[0]["other_folders"] == ["api"]  # the OTHER folder's friendly label
    api = _workset_member_lanes(rollup, "project:api:bbbb")
    assert api[0]["other_folders"] == ["webapp"]


def test_candidates_count_a_shared_session_in_each_folder_it_touched() -> None:
    rollup = {"sessions": [_wander_entry()]}
    candidates = {c["project_identity"]: c for c in workset_candidates(rollup)}
    assert set(candidates) == {"project:api:bbbb", "project:webapp:aaaa"}
    assert candidates["project:api:bbbb"]["session_count"] == 1
    assert candidates["project:webapp:aaaa"]["session_count"] == 1
    assert candidates["project:api:bbbb"]["label"] == "api"  # parsed from the identity


def test_summary_counts_shared_sessions_for_honest_disclosure() -> None:
    summary = summarize_members([
        _wander_entry(),
        {"client": "codex", "session_kind": "root", "project_identity": "project:webapp:aaaa",
         "project_identity_state": "explicit", "usage": {"total_tokens": 5}},
    ])
    assert summary["session_count"] == 2
    assert summary["shared_sessions"] == 1  # the wander session is counted here and in its other folder


def test_summary_sums_combined_session_duration() -> None:
    summary = summarize_members([
        {"client": "claude-code", "session_kind": "root", "duration_seconds": 3600, "usage": {}},
        {"client": "codex", "session_kind": "root", "duration_seconds": 1800, "usage": {}},
        {"client": "claude-code", "session_kind": "root", "usage": {}},  # no duration — skipped
    ])
    assert summary["combined_duration_seconds"] == 5400.0
    # None (never a fabricated zero) when nothing has a usable duration.
    assert summarize_members([{"client": "codex", "session_kind": "root", "usage": {}}])["combined_duration_seconds"] is None


def test_summary_is_a_labeled_partial_sum_when_a_member_is_unpriced() -> None:
    members = [
        {"client": "claude-code", "session_kind": "root",
         "usage": {"total_tokens": 1000, "estimated_cost_usd": 1.5, "cost_confidence": "estimated_from_tokens"}},
        {"client": "codex", "session_kind": "root", "usage": {"total_tokens": 500}},  # unpriced
    ]
    summary = summarize_members(members)
    assert summary["session_count"] == 2
    assert summary["estimated_cost_usd"] == 1.5
    assert summary["cost_complete"] is False
    assert summary["priced_sessions"] == 1
    assert summary["unpriced_sessions"] == 1
    assert summary["cost_basis"] == "sum_of_independent_receipts"


# --- API integration ----------------------------------------------------------


def _record_session_usage(
    service: SentinelService, *, client: str, session_id: str, project_dir: str, at: float, cost: float | None
) -> None:
    metadata = {
        "usage_source": "local_client_session_store",
        "usage_provenance": "agent_sentinel_local_usage_import",
        "client": client,
        "client_session_id": session_id,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "project_dir": project_dir,
        "started_at": at,
        "updated_at": at,
        "session_namespace_fingerprint": f"sha256:{client}-{session_id}",
        "identity_scope_state": "explicit",
        "source_namespace_fingerprint": f"sha256:{client}-{session_id}",
    }
    event = {
        "event_id": f"evt_usage_{client}_{session_id}",
        "created_at": at,
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "run_id": None,
        "provider": client,
        "model": "claude-opus-4-8" if client == "claude-code" else "gpt-5-codex",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 25,
        "usage_confidence": "client_reported",
        "cost_basis": "pricing_table",
        "metadata": metadata,
    }
    if cost is not None:
        event["estimated_cost_usd"] = cost
        event["cost_confidence"] = "estimated_from_tokens"
    service.record_event(event, trusted_usage_import=True)


def _seed_two_source_folder(tmp_path: Path) -> str:
    service = SentinelService(tmp_path)
    _record_session_usage(service, client="claude-code", session_id="cc1", project_dir="/tmp/webapp", at=1000.0, cost=1.5)
    _record_session_usage(service, client="claude-code", session_id="cc2", project_dir="/tmp/webapp", at=1100.0, cost=1.0)
    _record_session_usage(service, client="codex", session_id="cx1", project_dir="/tmp/webapp", at=1200.0, cost=0.9)
    _record_session_usage(service, client="claude-code", session_id="oo1", project_dir="/tmp/other", at=1300.0, cost=0.4)
    return _project_identity("/tmp/webapp")


def test_candidates_endpoint_exposes_folders_without_paths(tmp_path: Path) -> None:
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    resp = client.get("/v1/workset-candidates", headers=_auth())
    assert resp.status_code == 200
    candidates = {c["project_identity"]: c for c in resp.json()["candidates"]}
    assert identity in candidates
    webapp = candidates[identity]
    assert webapp["session_count"] == 3
    assert set(webapp["sources"]) == {"claude-code", "codex"}
    # No raw absolute path is ever exposed.
    assert "/tmp/webapp" not in resp.text


def test_v1_lane_requires_bearer_token(tmp_path: Path) -> None:
    _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    assert client.get("/v1/workset-candidates").status_code == 401
    assert client.get("/v1/worksets").status_code == 401
    assert client.post("/v1/worksets", json={"action": "create"}).status_code == 401


def test_create_list_detail_roundtrip_groups_cross_source(tmp_path: Path) -> None:
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    created = client.post(
        "/v1/worksets",
        headers=_auth(),
        json={"action": "create", "workset_id": "ws_web", "name": "tryairis.ai",
              "directory": identity, "expected_revision": 0},
    )
    assert created.status_code == 200, created.text
    assert created.json()["revision"] == 1

    listing = client.get("/v1/worksets", headers=_auth()).json()
    assert listing["total"] == 1
    card = listing["worksets"][0]
    assert card["name"] == "tryairis.ai"
    assert card["summary"]["session_count"] == 3
    assert {s["client"] for s in card["summary"]["sources"]} == {"claude-code", "codex"}
    # Cost is the labeled sum of the three priced members (1.5 + 1.0 + 0.9).
    assert round(card["summary"]["estimated_cost_usd"], 2) == 3.4
    assert card["summary"]["cost_complete"] is True

    detail = client.get("/v1/workset", headers=_auth(), params={"id": "ws_web"}).json()
    lanes = detail["sessions"]
    assert len(lanes) == 3
    assert {lane["client"] for lane in lanes} == {"claude-code", "codex"}
    # Lanes are ordered along the shared time axis.
    times = [lane["first_activity_at"] for lane in lanes]
    assert times == sorted(times)


def test_one_group_per_folder_second_create_conflicts(tmp_path: Path) -> None:
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    first = client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "create", "workset_id": "ws_a", "name": "web",
              "directory": identity, "expected_revision": 0},
    )
    assert first.status_code == 200
    # A DIFFERENT id onto the same folder is refused.
    dup = client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "create", "workset_id": "ws_b", "name": "web again",
              "directory": identity, "expected_revision": 0},
    )
    assert dup.status_code == 409
    # The candidate for that folder now points at the existing group.
    cands = {c["project_identity"]: c for c in
             client.get("/v1/workset-candidates", headers=_auth()).json()["candidates"]}
    assert cands[identity]["existing_workset_id"] == "ws_a"
    # A retry of the SAME group's create (same id, name, revision -> same
    # derived idempotency key) replays idempotently rather than 409-ing on the
    # duplicate-folder guard.
    retry = client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "create", "workset_id": "ws_a", "name": "web",
              "directory": identity, "expected_revision": 0},
    )
    assert retry.status_code == 200


def test_lanes_carry_hover_fields(tmp_path: Path) -> None:
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "create", "workset_id": "ws_web", "name": "web",
              "directory": identity, "expected_revision": 0},
    )
    detail = client.get("/v1/workset", headers=_auth(), params={"id": "ws_web"}).json()
    for lane in detail["sessions"]:
        for key in ("tool_calls", "steps", "checks", "checks_failed", "status", "session_key"):
            assert key in lane, key
        assert isinstance(lane["status"], str) and lane["status"]


def test_membership_is_live_new_session_joins_without_a_write(tmp_path: Path) -> None:
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "create", "workset_id": "ws_web", "name": "web",
              "directory": identity, "expected_revision": 0},
    )
    before = client.get("/v1/workset", headers=_auth(), params={"id": "ws_web"}).json()
    assert before["summary"]["session_count"] == 3
    # A brand-new session appears in the folder; no workset write happens.
    _record_session_usage(
        SentinelService(tmp_path), client="codex", session_id="cx2",
        project_dir="/tmp/webapp", at=1400.0, cost=0.2,
    )
    after = client.get("/v1/workset", headers=_auth(), params={"id": "ws_web"}).json()
    assert after["summary"]["session_count"] == 4


def test_stale_expected_revision_conflicts(tmp_path: Path) -> None:
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "create", "workset_id": "ws_web", "name": "web",
              "directory": identity, "expected_revision": 0},
    )
    stale = client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "rename", "workset_id": "ws_web", "name": "renamed",
              "expected_revision": 0},  # should be 1 now
    )
    assert stale.status_code == 409


def test_rename_and_delete_then_detail_is_404(tmp_path: Path) -> None:
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "create", "workset_id": "ws_web", "name": "web",
              "directory": identity, "expected_revision": 0},
    )
    renamed = client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "rename", "workset_id": "ws_web", "name": "airis",
              "expected_revision": 1},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "airis"
    deleted = client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "delete", "workset_id": "ws_web", "expected_revision": 2},
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get("/v1/workset", headers=_auth(), params={"id": "ws_web"}).status_code == 404
    assert client.get("/v1/worksets", headers=_auth()).json()["total"] == 0


def test_create_without_name_is_400(tmp_path: Path) -> None:
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    resp = client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "create", "workset_id": "ws_x", "directory": identity, "expected_revision": 0},
    )
    assert resp.status_code == 400


def test_idempotent_create_retry_returns_same_state(tmp_path: Path) -> None:
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    body = {"action": "create", "workset_id": "ws_web", "name": "web",
            "directory": identity, "expected_revision": 0, "idempotency_key": "make-web"}
    first = client.post("/v1/worksets", headers=_auth(), json=body)
    second = client.post("/v1/worksets", headers=_auth(), json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["event_id"] == second.json()["event_id"]
    assert client.get("/v1/worksets", headers=_auth()).json()["total"] == 1


def _forged_workset_metadata(**over) -> dict:
    from agentacct.worksets import (
        WORKSET_AUTHORITY_SCOPE,
        WORKSET_CONTRACT_KEY,
        WORKSET_CONTRACT_VERSION,
        WORKSET_EVENT_TYPE,
    )

    md = {
        "sentinel_semantic_kind": WORKSET_EVENT_TYPE,
        WORKSET_CONTRACT_KEY: WORKSET_CONTRACT_VERSION,
        "authority_scope": WORKSET_AUTHORITY_SCOPE,
        "authoritative_for_check_result": False,
        "actor": "dashboard-user",
        "workset_id": "ws_evil",
        "action": "create",
        "name": "Injected",
        "project_identity": "project:evil:0000",
        "expected_revision": 0,
        "revision": 1,
        "idempotency_key": "forge-key",
    }
    md.update(over)
    md["operation_digest"] = workset_operation_digest(
        workset_id=md["workset_id"], action=md["action"], name=md["name"],
        project_identity=md["project_identity"], expected_revision=md["expected_revision"],
    )
    return md


def test_forged_workset_via_record_event_is_stripped_not_trusted(tmp_path: Path) -> None:
    """A raw record_event caller cannot mint a trusted grouping (contract #3)."""
    from agentacct.worksets import (
        WORKSET_CONTRACT_KEY,
        WORKSET_EVENT_TYPE,
        WORKSET_SOURCE,
    )

    service = SentinelService(tmp_path)
    service.record_event(
        {
            "source": WORKSET_SOURCE,
            "event_type": WORKSET_EVENT_TYPE,
            "run_id": None,
            "metadata": _forged_workset_metadata(),
        }
    )
    events = service.list_all_events()
    proj = reduce_worksets(events)
    assert proj.active() == []  # the forged grouping never trusts
    row = next(e for e in events if isinstance(e.get("metadata"), dict)
               and e["metadata"].get("workset_id") == "ws_evil")
    # The audit row survives, but the reserved contract is gone and tombstoned.
    assert WORKSET_CONTRACT_KEY not in row["metadata"]
    assert row["metadata"].get("reserved_workset_provenance_stripped") is True


def test_forged_workset_never_reaches_the_v1_wire(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    service.record_event(
        {
            "source": "agent-chronicle-workset",
            "event_type": "workset_action",
            "run_id": None,
            "metadata": _forged_workset_metadata(),
        }
    )
    client = _app(tmp_path)
    assert client.get("/v1/worksets", headers=_auth()).json()["total"] == 0
    assert client.get("/v1/workset", headers=_auth(), params={"id": "ws_evil"}).status_code == 404


def _inject_trusted_workset(service: SentinelService, **md) -> None:
    """Append a hand-stamped trusted workset event straight to the ledger.

    Simulates a ledger that already holds a conflicting trusted pair (a merge,
    a future migration, or a corrupt store) so the defensive in-loop invalid
    check and the detail/list agreement can be exercised.
    """
    from agentacct.service import mark_trusted_workset

    digest = workset_operation_digest(
        workset_id=md["workset_id"], action=md["action"], name=md.get("name"),
        project_identity=md.get("project_identity"), expected_revision=md["expected_revision"],
    )
    event = mark_trusted_workset(
        {"run_id": None, "metadata": {**md, "operation_digest": digest}}
    )
    recorded = service._prepare_recorded_event(event)
    service._append_ledger_events([recorded])


def test_invalidated_chain_is_hidden_and_conflicts_on_replay(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    # Two trusted create events reusing one idempotency key -> reducer poisons both.
    _inject_trusted_workset(
        service, workset_id="ws_conf", action="create", name="A",
        project_identity="project:a:1111", expected_revision=0, revision=1, idempotency_key="dup",
    )
    _inject_trusted_workset(
        service, workset_id="ws_conf2", action="create", name="B",
        project_identity="project:b:2222", expected_revision=0, revision=1, idempotency_key="dup",
    )
    proj = reduce_worksets(service.list_all_events())
    assert "ws_conf" in proj.invalid and "ws_conf2" in proj.invalid

    client = _app(tmp_path)
    # The list hides invalid chains; the detail must agree (no stale success).
    assert client.get("/v1/worksets", headers=_auth()).json()["total"] == 0
    assert client.get("/v1/workset", headers=_auth(), params={"id": "ws_conf"}).status_code == 404

    # A replay against the poisoned chain conflicts rather than returning stale.
    with pytest.raises(WorksetConflict):
        service.record_workset_action(
            action="create", workset_id="ws_conf", name="A",
            project_identity="project:a:1111", expected_revision=0, idempotency_key="dup",
        )


def test_worksets_never_touch_task_receipts(tmp_path: Path) -> None:
    """Creating a grouping must not change the underlying Tasks' receipts."""
    identity = _seed_two_source_folder(tmp_path)
    client = _app(tmp_path)
    before = client.get("/v1/tasks", headers=_auth()).json()
    client.post(
        "/v1/worksets", headers=_auth(),
        json={"action": "create", "workset_id": "ws_web", "name": "web",
              "directory": identity, "expected_revision": 0},
    )
    after = client.get("/v1/tasks", headers=_auth()).json()
    assert before["total"] == after["total"]
    assert [t["task_id"] for t in before["tasks"]] == [t["task_id"] for t in after["tasks"]]
