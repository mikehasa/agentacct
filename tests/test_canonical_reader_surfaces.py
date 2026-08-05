"""Phase 4.2 — Work/Task/Sessions canonical read surfaces.

Extends the 4.1 reader conventions (test_canonical_reader.py): stores are
import-produced candidates promoted to role='live' in tmp_path, the read flag
is pinned off suite-wide by conftest and exercised through the constructor
override or explicit monkeypatch.setenv.

The load-bearing semantics under test:
- rm_task_current is a REBUILD-ONLY projection: never-built / not-current
  refuse service (an empty task list must not impersonate "no tasks"),
  stale-but-current serves WITH built_through vs canonical_sequence labels —
  the same three gates as rm_usage_day in 4.1.
- sessions is a BASE FACT TABLE: it serves even when the read models were
  never built (the live shadow store shape).
- Canonical public task ids are importer-minted and DISJOINT from the v1
  HMAC id namespace: in canonical mode an unknown id is an honest 404,
  never silently answered from v1.
- Model gaps are labeled, honest nulls preserved (missing ≠ 0), and every
  unavailability is the LABELED v1 fallback — never a 500, never silent
  wrong data. Flag off = byte-identical.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentacct.canonical_read import (
    CANONICAL_READ_ENV,
    CanonicalReadRuntime,
    CanonicalReadUnavailable,
)

from test_canonical_live_writer import _run_importer, _usage_event
from test_canonical_reader import (
    _api_client,
    _imported_live_store,
    _promote_candidate_to_live,
    _trusted,
    _undated_usage_event,
    _write_ledger,
)

LIVE_STORE_FILENAME = "chronicle.sqlite3"


# --- fixture events ---------------------------------------------------------


def _claim_event(
    *,
    event_id: str,
    session_id: str,
    created_at: float,
    status: str = "started",
    title: str = "root task",
    section_id: str = "sec-root",
    kind: str | None = None,
    parent: str | None = None,
    started_at: float | None = None,
    namespace: str = "fp-32parity",
) -> dict[str, Any]:
    """A work-claim-bearing section event (the v1 Work lane's native shape)."""

    metadata: dict[str, Any] = {
        "client": "claude-code",
        "client_session_id": session_id,
        "section_id": section_id,
        "section_status": status,
        "section_title": title,
        "sentinel_semantic_kind": "section",
        "source_namespace_fingerprint": namespace,
        "updated_at": created_at,
    }
    if kind is not None:
        metadata["client_session_kind"] = kind
    if parent is not None:
        metadata["parent_client_session_id"] = parent
    if started_at is not None:
        metadata["started_at"] = started_at
    return {
        "event_id": event_id,
        "event_type": f"section_{status}",
        "source": "test-suite",
        "created_at": created_at,
        "metadata": metadata,
    }


def _work_ledger_events() -> list[dict[str, Any]]:
    """Two tasks, three sessions, honest nulls on both sides.

    - claude-code root p-1 (anchored task): started + completed claim with a
      title, a lineage-valid child c-1, NO usage → token fields must stay
      None (missing ≠ 0), projected_state reported_complete, session_count 2.
    - codex root u-1 (anchored task): usage only, NO work claim → title None,
      projected_state active, tokens 100/50/150.
    """

    return [
        _claim_event(
            event_id="evt_p1",
            session_id="p-1",
            created_at=1_700_000_000.0,
            status="started",
            kind="root",
            started_at=1_699_999_000.0,
        ),
        _claim_event(
            event_id="evt_p2",
            session_id="p-1",
            created_at=1_700_000_600.0,
            status="completed",
        ),
        _claim_event(
            event_id="evt_c1",
            session_id="c-1",
            created_at=1_700_000_500.0,
            status="checkpoint",
            title="child work",
            section_id="sec-child",
            kind="child",
            parent="p-1",
        ),
        _usage_event(
            event_id="evt_u1",
            session_id="u-1",
            created_at=1_700_100_000.0,
            input_tokens=100,
            output_tokens=50,
        ),
    ]


def _tasks_by_state(read: Any) -> dict[str, Any]:
    return {task.projected_state: task for task in read.tasks}


# --- runtime: task list -----------------------------------------------------


def test_task_list_read_serves_imported_tasks_with_honest_nulls(tmp_path):
    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="tasks")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = runtime.task_list_read()
    assert len(read.tasks) == 2
    by_state = _tasks_by_state(read)
    claude_task = by_state["reported_complete"]
    codex_task = by_state["active"]

    assert claude_task.title == "root task"
    assert claude_task.session_count == 2, "the lineage-valid child joins its root's task"
    assert claude_task.usage_measurement_count == 0
    assert claude_task.input_tokens is None, "no usage means None, never 0"
    assert claude_task.output_tokens is None
    assert claude_task.total_tokens is None

    assert codex_task.title is None, "no work claim means no title, never invented"
    assert codex_task.session_count == 1
    assert codex_task.input_tokens == 100
    assert codex_task.output_tokens == 50
    assert codex_task.total_tokens == 150

    assert read.truncated is False
    assert read.projection["stale"] is False
    assert read.projection["projection_name"] == "rm_task_current"
    assert read.store["store_role"] == "live"
    # Identity labels resolve each task's primary session.
    claude_primary = read.sessions[claude_task.primary_session_id]
    assert claude_primary.client_session_id == "p-1"
    assert read.sources[claude_primary.source_instance_id].client == "claude-code"
    codex_primary = read.sessions[codex_task.primary_session_id]
    assert codex_primary.client_session_id == "u-1"
    assert read.sources[codex_primary.source_instance_id].client == "codex"
    # Newest-first: the codex task carries the later activity.
    assert read.tasks[0].public_task_id == codex_task.public_task_id
    assert runtime.status()["served"] == 1


def test_task_list_read_refuses_never_built_projection(tmp_path):
    from agentacct.canonical.sqlite import CanonicalStore

    store_root = tmp_path / "store-shadow"
    store_root.mkdir()
    CanonicalStore.create_live(store_root).close()
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        runtime.task_list_read()
    assert excinfo.value.reason == "projection_never_built"
    assert "rm_task_current" in excinfo.value.detail


def test_task_list_read_refuses_not_current_but_usage_still_serves(tmp_path):
    """The two projections gate independently: a failed rm_task_current
    refuses the task surfaces while rm_usage_day keeps serving."""

    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="notcur-t")
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute(
            "UPDATE projection_generations SET state = 'failed' "
            "WHERE projection_name = 'rm_task_current'"
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        runtime.task_list_read()
    assert excinfo.value.reason == "projection_not_current"
    usage = runtime.usage_day_read(start_day="1970-01-02", end_day="9999-12-31")
    assert usage.rows, "rm_usage_day is untouched and must keep serving"


def test_task_list_read_stale_projection_is_labeled_not_refused(tmp_path):
    from agentacct.canonical.sqlite import CanonicalStore

    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="stale-t")
    store = CanonicalStore.open_live(store_root)
    try:
        repository = store.repository()
        with store.transaction(write=True):
            repository._advance_sequence(store.connection)
    finally:
        store.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = runtime.task_list_read()
    assert read.projection["stale"] is True
    assert read.projection["pending_writes"] >= 1
    assert len(read.tasks) == 2, "stale-but-current serves, with the label"


def test_task_list_truncation_is_labeled(tmp_path):
    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="trunc-t")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = runtime.task_list_read(limit=1)
    assert len(read.tasks) == 1
    assert read.truncated is True


# --- runtime: task detail ---------------------------------------------------


def test_task_detail_read_found_and_honest_miss(tmp_path):
    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="detail")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    listed = runtime.task_list_read()
    target = listed.tasks[0]
    detail = runtime.task_detail_read(target.public_task_id)
    assert detail.task == target
    assert detail.sessions[target.primary_session_id].client_session_id == "u-1"

    miss = runtime.task_detail_read("task_" + "0" * 32)
    assert miss.task is None, "an unknown id is an honest miss, not an error"
    status = runtime.status()
    assert status["served"] == 3, "a miss on a healthy store is a served read"
    assert status["unavailable"] == 0


def test_task_detail_read_refuses_never_built_instead_of_missing(tmp_path):
    """Gate ordering: a store whose rm_task_current was never built must
    refuse — reporting 'no such task' would impersonate an empty store."""

    from agentacct.canonical.sqlite import CanonicalStore

    store_root = tmp_path / "store-shadow"
    store_root.mkdir()
    CanonicalStore.create_live(store_root).close()
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        runtime.task_detail_read("task_" + "0" * 32)
    assert excinfo.value.reason == "projection_never_built"


# --- runtime: session list --------------------------------------------------


def test_session_list_read_serves_identity_and_validated_lineage(tmp_path):
    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="sessions")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = runtime.session_list_read()
    assert len(read.sessions) == 3
    by_client_session = {session.client_session_id: session for session in read.sessions}
    assert set(by_client_session) == {"p-1", "c-1", "u-1"}
    assert by_client_session["p-1"].session_kind == "root"
    assert by_client_session["c-1"].session_kind == "child"
    # Newest activity first.
    assert read.sessions[0].client_session_id == "u-1"
    # Source identity labels.
    sources = read.sources
    assert sources[by_client_session["p-1"].source_instance_id].client == "claude-code"
    assert sources[by_client_session["u-1"].source_instance_id].client == "codex"
    # The child's VALIDATED lineage edge resolves to p-1 (in-page lookup).
    child_id = by_client_session["c-1"].session_id
    parent_id, relation = read.lineage_edges[child_id]
    assert relation == "parent"
    assert parent_id == by_client_session["p-1"].session_id
    assert by_client_session["p-1"].session_id not in read.parent_sessions, (
        "in-page parents are not refetched"
    )
    assert read.truncated is False


def test_session_list_read_serves_when_projections_never_built(tmp_path):
    """sessions is a base fact table: deleting the projection generations
    must refuse the task surfaces while sessions keep serving."""

    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="basefact")
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute("DELETE FROM projection_generations")
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable):
        runtime.task_list_read()
    read = runtime.session_list_read()
    assert len(read.sessions) == 3


def test_session_list_read_truncation_and_out_of_page_parent(tmp_path):
    """limit=1 returns only the newest session; a truncated page still
    resolves nothing it did not fetch (u-1 has no parent edge)."""

    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="trunc-s")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = runtime.session_list_read(limit=1)
    assert len(read.sessions) == 1
    assert read.truncated is True
    assert read.sessions[0].client_session_id == "u-1"
    assert read.lineage_edges == {}

    # limit=2 fetches u-1 and p-1... the child c-1 is outside the page, so no
    # edge rides along (edges are looked up for the PAGE's children only).
    wider = runtime.session_list_read(limit=2)
    shown = {session.client_session_id for session in wider.sessions}
    assert shown == {"u-1", "p-1"}
    assert wider.lineage_edges == {}, "p-1 is a parent, not a child — no edge for it"


def test_session_list_read_undated_session_sorts_last_with_honest_nones(tmp_path):
    events = [
        *_work_ledger_events(),
        _undated_usage_event(event_id="evt_und", session_id="s-undated"),
    ]
    store_root = _imported_live_store(tmp_path, events, name="undated-s")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = runtime.session_list_read()
    assert len(read.sessions) == 4
    tail = read.sessions[-1]
    assert tail.client_session_id == "s-undated"
    assert tail.sort_activity_at_us == 0, "no activity timestamp sorts at the bottom"
    assert tail.last_activity_at_us is None, "the display value stays honestly None"


# --- repository: by-ids batch bounds ---------------------------------------


def test_by_ids_lookups_validate_batches(tmp_path):
    from agentacct.canonical.sqlite import CanonicalStore

    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="batch")
    store = CanonicalStore.open_live(store_root, read_only=True)
    try:
        repository = store.repository()
        assert repository.get_sessions_by_ids([]) == {}
        assert repository.get_source_instances_by_ids([]) == {}
        assert repository.valid_lineage_edges([]) == {}
        with pytest.raises(ValueError):
            repository.get_sessions_by_ids([True])
        with pytest.raises(ValueError):
            repository.get_sessions_by_ids(["1"])  # type: ignore[list-item]
        with pytest.raises(ValueError):
            repository.valid_lineage_edges(list(range(2_001)))
    finally:
        store.close()


# --- endpoints: flag off untouched ------------------------------------------


def _endpoint_store(tmp_path: Path, events: list[dict[str, Any]], *, name: str) -> Path:
    """events.jsonl + promoted canonical store in the SAME api store dir."""

    prepared = _trusted(events)
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, prepared)
    candidate = _run_importer(tmp_path, prepared, name=name)
    _promote_candidate_to_live(candidate, store_dir)
    return store_dir


def test_tasks_and_sessions_flag_off_are_untouched(tmp_path):
    store_dir = _endpoint_store(tmp_path, _work_ledger_events(), name="off")
    client = _api_client(store_dir)

    tasks = client.get("/tasks").json()
    assert tasks["schema_version"] == "agent-chronicle.task-projection.v1"
    assert "canonical_read" not in tasks
    assert tasks["summary"]["task_count"] == 3

    sessions = client.get("/sessions").json()
    assert sessions["schema_version"] == "agent-sentinel.work-ledger.v2"
    assert "canonical_read" not in sessions
    assert isinstance(sessions["total_sessions"], int)

    health = client.get("/health").json()
    assert health["canonical_read"]["enabled"] is False
    assert health["canonical_read"]["attempts"] == 0


# --- endpoints: flag on serves canonical -------------------------------------


def test_tasks_flag_on_serves_canonical(tmp_path, monkeypatch):
    store_dir = _endpoint_store(tmp_path, _work_ledger_events(), name="tasks-on")
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(store_dir)
    payload = client.get("/tasks").json()
    assert payload["schema_version"] == "agent-chronicle.task-projection.v2-canonical"
    label = payload["canonical_read"]
    assert label["active"] is True
    assert label["source"] == "canonical"
    assert label["projection"]["stale"] is False
    assert label["truncated"] is False
    assert payload["summary"]["task_count_shown"] == 2
    assert "csrf_token" not in payload, "the v1 disposition-form token has no canonical analog"
    gaps = payload["model_gaps"]
    assert "findings" in gaps["not_represented"]
    assert "finding_dispositions" in gaps["not_represented"]
    assert "planned_control_tasks" in gaps["not_represented"]
    assert "generic_notes" in gaps["not_represented"]

    by_state = {entry["projected_state"]: entry for entry in payload["tasks"]}
    claude_entry = by_state["reported_complete"]
    codex_entry = by_state["active"]
    assert claude_entry["title"] == "root task"
    assert claude_entry["session_count"] == 2
    assert claude_entry["input_tokens"] is None, "missing usage stays None on the wire"
    assert claude_entry["output_tokens"] is None
    assert claude_entry["total_tokens"] is None
    assert "finding_count" not in claude_entry, (
        "no canonical writer records findings; serving the projection's 0 "
        "would let missing impersonate none"
    )
    assert "finding_count" not in codex_entry
    assert claude_entry["primary_session"]["client"] == "claude-code"
    assert claude_entry["primary_session"]["client_session_id"] == "p-1"
    assert codex_entry["title"] is None
    assert codex_entry["input_tokens"] == 100
    assert codex_entry["total_tokens"] == 150
    assert codex_entry["primary_session"]["client"] == "codex"
    assert codex_entry["last_activity_at"] is not None
    assert codex_entry["last_activity_at"].endswith("+00:00")

    health = client.get("/health").json()
    assert health["canonical_read"]["served"] >= 1


def test_tasks_flag_on_store_absent_falls_back_labeled(tmp_path, monkeypatch):
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, _trusted(_work_ledger_events()))
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(store_dir)
    payload = client.get("/tasks").json()
    assert payload["schema_version"] == "agent-chronicle.task-projection.v1"
    assert payload["canonical_read"]["active"] is False
    assert payload["canonical_read"]["source"] == "v1_fallback"
    assert payload["canonical_read"]["reason"] == "store_absent"
    assert payload["summary"]["task_count"] == 3, "the fallback is the full v1 surface"


def test_tasks_flag_on_never_built_projection_falls_back_labeled(tmp_path, monkeypatch):
    from agentacct.canonical.sqlite import CanonicalStore

    store_dir = tmp_path / "store"
    _write_ledger(store_dir, _trusted(_work_ledger_events()))
    CanonicalStore.create_live(store_dir).close()
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(store_dir)
    payload = client.get("/tasks").json()
    assert payload["schema_version"] == "agent-chronicle.task-projection.v1"
    assert payload["canonical_read"]["reason"] == "projection_never_built"


def test_tasks_payload_build_crash_falls_back_labeled(tmp_path, monkeypatch):
    import agentacct.api as api_module

    store_dir = _endpoint_store(tmp_path, _work_ledger_events(), name="tasks-crash")
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("task payload exploded")

    monkeypatch.setattr(api_module, "_canonical_task_projection_payload", _boom)
    client = _api_client(store_dir)
    response = client.get("/tasks")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "agent-chronicle.task-projection.v1"
    assert payload["canonical_read"]["reason"] == "error"
    assert "task payload exploded" in payload["canonical_read"]["detail"]
    health = client.get("/health").json()
    assert health["canonical_read"]["errors"] >= 1


def test_sessions_flag_on_serves_canonical(tmp_path, monkeypatch):
    store_dir = _endpoint_store(tmp_path, _work_ledger_events(), name="sessions-on")
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(store_dir)
    payload = client.get("/sessions").json()
    assert payload["schema_version"] == "agent-sentinel.session-rollup.v2-canonical"
    assert payload["canonical_read"]["active"] is True
    assert payload["session_count_shown"] == 3
    assert payload["total_sessions"] is None, "full-store count is not computed, not 0"
    assert payload["total_sessions_reason"] == "not_computed_in_canonical_read"
    by_session = {entry["client_session_id"]: entry for entry in payload["sessions"]}
    assert set(by_session) == {"p-1", "c-1", "u-1"}
    assert by_session["u-1"]["client"] == "codex"
    assert by_session["p-1"]["client"] == "claude-code"
    child = by_session["c-1"]
    assert child["parent"]["client_session_id"] == "p-1"
    assert child["parent"]["relation"] == "parent"
    assert by_session["p-1"]["parent"] is None
    assert "observed_unvalidated_lineage" in payload["model_gaps"]["not_served"]

    limited = client.get("/sessions", params={"limit": 1}).json()
    assert limited["session_count_shown"] == 1
    assert limited["canonical_read"]["truncated"] is True

    filtered = client.get("/sessions", params={"client": "codex"}).json()
    assert filtered["ignored_html_params"] == ["client"], (
        "HTML filter params stay ignored-with-a-label in canonical mode"
    )


def test_sessions_flag_on_store_absent_falls_back_labeled(tmp_path, monkeypatch):
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, _trusted(_work_ledger_events()))
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(store_dir)
    payload = client.get("/sessions", params={"client": "codex"}).json()
    assert payload["schema_version"] == "agent-sentinel.work-ledger.v2"
    assert payload["canonical_read"]["reason"] == "store_absent"
    assert payload["ignored_html_params"] == ["client"]
    assert isinstance(payload["total_sessions"], int)


def test_sessions_payload_build_crash_falls_back_labeled(tmp_path, monkeypatch):
    import agentacct.api as api_module

    store_dir = _endpoint_store(tmp_path, _work_ledger_events(), name="sessions-crash")
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("session payload exploded")

    monkeypatch.setattr(api_module, "_canonical_session_rollup_payload", _boom)
    client = _api_client(store_dir)
    response = client.get("/sessions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "agent-sentinel.work-ledger.v2"
    assert payload["canonical_read"]["reason"] == "error"
    assert "session payload exploded" in payload["canonical_read"]["detail"]


# --- review-round tests (adversarial review of the 4.2 diff) ----------------


def test_continuation_lineage_serves_with_its_relation_and_task_side_agrees(tmp_path):
    """Writers mint a continuation child's single VALID edge with
    relation='continuation' (relation mirrors the session kind); the sessions
    surface must serve that validated lineage — filtering to
    relation='parent' rendered it as parent-less while the task surface
    counted the same child (the review's contradiction)."""

    events = [
        _claim_event(
            event_id="evt_p1", session_id="p-1", created_at=1_700_000_000.0, kind="root"
        ),
        _claim_event(
            event_id="evt_k1",
            session_id="k-1",
            created_at=1_700_000_400.0,
            status="checkpoint",
            title="continued work",
            section_id="sec-cont",
            kind="continuation",
            parent="p-1",
        ),
    ]
    store_root = _imported_live_store(tmp_path, events, name="continuation")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = runtime.session_list_read()
    by_client_session = {s.client_session_id: s for s in read.sessions}
    child_id = by_client_session["k-1"].session_id
    parent_id, relation = read.lineage_edges[child_id]
    assert relation == "continuation"
    assert parent_id == by_client_session["p-1"].session_id
    tasks = runtime.task_list_read()
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].session_count == 2, (
        "the task closure and the sessions surface must agree on this lineage"
    )


def test_rejected_edges_are_withheld_from_lineage(tmp_path):
    """Only validation_state='valid' rows serve — pinned behaviorally (the
    review proved the filter had zero coverage: deleting it passed the
    whole suite)."""

    store_root = _imported_live_store(tmp_path, _work_ledger_events(), name="rejected")
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute("UPDATE session_edges SET validation_state = 'rejected'")
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = runtime.session_list_read()
    assert read.lineage_edges == {}, "a rejected edge is not validated lineage"


def _child_newer_events() -> list[dict[str, Any]]:
    return [
        _claim_event(
            event_id="evt_p1", session_id="p-1", created_at=1_700_000_000.0, kind="root"
        ),
        _claim_event(
            event_id="evt_c9",
            session_id="c-9",
            created_at=1_700_900_000.0,
            status="checkpoint",
            title="newer child",
            section_id="sec-child",
            kind="child",
            parent="p-1",
        ),
    ]


def test_out_of_page_parent_is_fetched_and_resolved(tmp_path, monkeypatch):
    """A child NEWER than its parent puts the parent outside a limit=1 page:
    the runtime must fetch the referenced parent and the payload must
    resolve its identity (the branch was unreachable under the original
    fixture geometry)."""

    store_root = _imported_live_store(tmp_path, _child_newer_events(), name="outpage")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = runtime.session_list_read(limit=1)
    assert read.sessions[0].client_session_id == "c-9"
    child_id = read.sessions[0].session_id
    parent_id, relation = read.lineage_edges[child_id]
    assert relation == "parent"
    parent = read.parent_sessions[parent_id]
    assert parent.client_session_id == "p-1"
    assert read.sources[parent.source_instance_id].client == "claude-code"

    store_dir = _endpoint_store(tmp_path, _child_newer_events(), name="outpage-api")
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(store_dir)
    payload = client.get("/sessions", params={"limit": 1}).json()
    assert payload["schema_version"] == "agent-sentinel.session-rollup.v2-canonical"
    entry = payload["sessions"][0]
    assert entry["client_session_id"] == "c-9"
    assert entry["parent"]["client_session_id"] == "p-1"
    assert entry["parent"]["identity_resolved"] is True
    assert entry["parent"]["client"] == "claude-code"


# --- v1 vs canonical cross-checks on the same ledger -------------------------


def test_session_sets_agree_and_task_grouping_divergence_is_the_decided_one(
    tmp_path, monkeypatch
):
    """Session identity sets must agree exactly. Task GROUPING may not:
    c-1's parent pointer rides only in section-event metadata, which the
    canonical session absorb models (valid edge → c-1 joins p-1's task) but
    v1's session rollup does not consume (no usage row, no hook observation
    → c-1 is its own root task). 3 v1 tasks vs 2 canonical tasks on this
    ledger is therefore the canonical model representing MORE lineage, not a
    reader bug — the decided divergence class from the 4.1 recon (compare
    what both models measure; label what only one does). The
    mutually-visible-lineage parity case is the test below."""

    store_dir = _endpoint_store(tmp_path, _work_ledger_events(), name="parity")

    v1_client = _api_client(store_dir)
    v1_tasks = v1_client.get("/tasks").json()
    v1_sessions = v1_client.get("/sessions").json()

    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    canonical_client = _api_client(store_dir)
    canonical_tasks = canonical_client.get("/tasks").json()
    canonical_sessions = canonical_client.get("/sessions").json()

    v1_keys = {
        (entry["client"], entry["client_session_id"])
        for entry in v1_sessions["sessions"]
    }
    canonical_keys = {
        (entry["client"], entry["client_session_id"])
        for entry in canonical_sessions["sessions"]
    }
    assert canonical_keys == v1_keys == {
        ("claude-code", "p-1"),
        ("claude-code", "c-1"),
        ("codex", "u-1"),
    }
    assert v1_tasks["summary"]["task_count"] == 3
    assert canonical_tasks["summary"]["task_count_shown"] == 2
    canonical_states = {entry["projected_state"] for entry in canonical_tasks["tasks"]}
    assert canonical_states == {"reported_complete", "active"}


def test_task_counts_agree_when_lineage_is_visible_to_both_models(tmp_path, monkeypatch):
    """When the parent pointer rides on usage rows — the lane BOTH models
    consume — v1 and canonical group the child under the same single task."""

    parent = _usage_event(event_id="evt_a1", session_id="w-p", created_at=1_700_000_000.0)
    child = _usage_event(event_id="evt_a2", session_id="w-c", created_at=1_700_050_000.0)
    child["metadata"]["client_session_kind"] = "child"
    child["metadata"]["parent_client_session_id"] = "w-p"
    store_dir = _endpoint_store(tmp_path, [parent, child], name="parity-usage")

    v1_client = _api_client(store_dir)
    v1_tasks = v1_client.get("/tasks").json()
    assert v1_tasks["summary"]["task_count"] == 1

    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    canonical_client = _api_client(store_dir)
    canonical_tasks = canonical_client.get("/tasks").json()
    assert canonical_tasks["summary"]["task_count_shown"] == 1
    task_entry = canonical_tasks["tasks"][0]
    assert task_entry["session_count"] == 2
    assert task_entry["primary_session"]["client_session_id"] == "w-p"

    canonical_sessions = canonical_client.get("/sessions").json()
    child_entry = next(
        entry
        for entry in canonical_sessions["sessions"]
        if entry["client_session_id"] == "w-c"
    )
    assert child_entry["parent"]["client_session_id"] == "w-p"
    assert child_entry["parent"]["relation"] == "parent"
