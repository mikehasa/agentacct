"""Data-lane regressions that survived the HTML dashboard retirement.

The HTML display layer (page routes, form handlers, page-twin JSON routes)
was deliberately retired; this file keeps only the coverage that guards the
JSON data lanes it used to assert alongside the pages: the /tasks projection
(root-task grouping, run-id nesting, honest usage subtotals), the /sessions
JSON shape, /health branding, the no-live-scan contract for JSON reads, and
the work-item display-title helper that still backs kept projections.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

from fastapi.testclient import TestClient

import agentacct.api as api_module
from agentacct.api import create_local_api_app
from agentacct.service import SentinelService


def _trusted_usage(
    store_root,
    *,
    session,
    client="codex",
    input_tokens=100,
    output_tokens=25,
    cached_input_tokens=50,
    cost=0.01,
    cost_confidence="estimated_from_tokens",
    model="gpt-5.5",
    started_at=None,
    project_dir=None,
    session_kind=None,
    parent_session=None,
    cache_creation_input_tokens=None,
    cache_read_input_tokens=None,
    cache_creation_reported=None,
    cache_read_reported=None,
):
    if cache_creation_reported is None:
        cache_creation_reported = client != "codex"
    if cache_read_reported is None:
        cache_read_reported = True
    if cache_creation_reported and cache_creation_input_tokens is None:
        cache_creation_input_tokens = 0
    if cache_read_reported and cache_read_input_tokens is None:
        cache_read_input_tokens = cached_input_tokens
    metadata = {
        "usage_source": "local_client_session_store",
        "usage_update_semantics": "cumulative_snapshot",
        "client": client,
        "client_session_id": session,
        "cached_input_tokens": cached_input_tokens,
        "cache_creation_tokens_reported": cache_creation_reported,
        "cache_read_tokens_reported": cache_read_reported,
    }
    if started_at is not None:
        metadata["started_at"] = started_at
        metadata["updated_at"] = started_at
    if project_dir is not None:
        metadata["project_dir"] = project_dir
    if session_kind is not None:
        metadata["client_session_kind"] = session_kind
    if parent_session is not None:
        metadata["parent_client_session_id"] = parent_session
    if cache_creation_input_tokens is not None:
        metadata["cache_creation_input_tokens"] = cache_creation_input_tokens
    if cache_read_input_tokens is not None:
        metadata["cache_read_input_tokens"] = cache_read_input_tokens
    return SentinelService(store_root).record_event(
        {
            "source": f"{client}-local-session-import",
            "event_type": "model_usage",
            "provider": client,
            "model": model,
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_cost_usd": cost,
            "usage_confidence": "client_reported",
            "cost_confidence": cost_confidence,
            "metadata": metadata,
        },
        trusted_usage_import=True,
    )


def _client(store_root, **kwargs):
    return TestClient(create_local_api_app(store_dir=store_root, **kwargs))


# ---------------------------------------------------------------------------
# Work-item display title (helper still backs kept projections)
# ---------------------------------------------------------------------------


def test_titleless_agent_step_never_reads_not_reported_work():
    # A completed section the agent recorded with no title (title aliased to the
    # work_id) and kind "unknown" must NOT render "Not reported work" next to the
    # "Agent reported" tag — that reads as a self-contradiction.
    item = {
        "work_id": "w-123",
        "section_id": "s-1",
        "title": "w-123",
        "kind": "unknown",
        "client": "claude-code",
    }
    label = api_module._work_item_display_title(item)
    assert "Not reported" not in label
    assert label == "Untitled step · Claude Code"
    # The agent's own summary is preferred when present.
    assert (
        api_module._work_item_display_title({**item, "summary": "Restore context and read PROGRESS"})
        == "Restore context and read PROGRESS"
    )
    # A meaningful kind reads as a normal step.
    assert api_module._work_item_display_title({**item, "kind": "testing"}) == "Testing step"


# ---------------------------------------------------------------------------
# Performance contract (PRD §8): saved-data JSON reads never run the live scan
# ---------------------------------------------------------------------------


def test_json_data_lanes_never_run_live_scan(tmp_path, monkeypatch):
    """Saved-data JSON reads render without scanning client logs."""

    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="no-scan-session")

    def _boom(*args, **kwargs):
        raise AssertionError("the live client-log scan must not run for saved-data reads")

    monkeypatch.setattr(api_module, "_discover_local_usage", _boom)
    monkeypatch.setattr(api_module, "_discover_local_usage_sources", _boom)
    client = _client(store_root)

    assert client.get("/sessions").status_code == 200
    assert client.get("/tasks").status_code == 200
    assert client.get("/overview").status_code == 200


# ---------------------------------------------------------------------------
# /tasks projection: lineage grouping + honest usage subtotals
# ---------------------------------------------------------------------------


def test_tasks_projection_groups_root_child_and_internal_as_one_task(tmp_path):
    store_root = tmp_path / "state"
    now = 1_750_000_000.0
    _trusted_usage(
        store_root,
        session="lineage-root-session",
        session_kind="root",
        input_tokens=100,
        output_tokens=25,
        cached_input_tokens=0,
        model="gpt-root",
        started_at=now,
    )
    _trusted_usage(
        store_root,
        session="lineage-child-session",
        session_kind="child",
        parent_session="lineage-root-session",
        input_tokens=20,
        output_tokens=5,
        cached_input_tokens=0,
        model="gpt-child",
        started_at=now + 1,
    )
    _trusted_usage(
        store_root,
        session="lineage-review-session",
        session_kind="internal",
        parent_session="lineage-child-session",
        input_tokens=8,
        output_tokens=2,
        cached_input_tokens=0,
        model="gpt-review",
        started_at=now + 2,
    )
    client = _client(store_root)

    task_payload = client.get("/tasks").json()
    assert task_payload["summary"]["task_count"] == 1
    task = task_payload["tasks"][0]
    assert task["session_count"] == 3
    assert task["child_count"] == 1
    assert task["internal_count"] == 1
    assert task["usage"]["fresh_tokens"] == 125
    assert task["usage"]["excluded_non_additive_rows"] == 2
    assert len(client.get("/sessions").json()["sessions"]) == 3


def test_tasks_projection_uses_root_task_boundary_and_nests_run_id_steps(tmp_path):
    store_root = tmp_path / "state"
    now = 1_750_000_000.0
    _trusted_usage(
        store_root,
        session="long-root-session",
        session_kind="root",
        input_tokens=40,
        output_tokens=10,
        cached_input_tokens=0,
        model="gpt-root",
        started_at=now,
    )
    _trusted_usage(
        store_root,
        session="linked-review-session",
        session_kind="internal",
        parent_session="long-root-session",
        model="gpt-review",
        started_at=now + 1,
    )
    _trusted_usage(
        store_root,
        session="unlinked-sibling-session",
        session_kind="child",
        parent_session="long-root-session",
        input_tokens=24,
        output_tokens=6,
        cached_input_tokens=0,
        model="gpt-sibling",
        started_at=now + 2,
    )
    service = SentinelService(store_root)
    service.record_event(
        {
            "source": "codex",
            "run_id": "semantic-run-one",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "linked-step",
                "section_status": "completed",
                "section_title": "Linked review step",
                "client": "codex",
                "client_session_id": "linked-review-session",
            },
        }
    )
    service.record_event(
        {
            "source": "codex",
            "run_id": "semantic-run-one",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "idless-step",
                "section_status": "completed",
                "section_title": "Id-less sibling step",
                "client": "codex",
            },
        }
    )

    client = _client(store_root)
    projection = client.get("/tasks").json()
    assert projection["summary"]["task_count"] == 1
    assert projection["summary"]["associated_work_count"] == 2
    assert projection["summary"]["unresolved_work_count"] == 0
    task = projection["tasks"][0]
    assert task["session_count"] == 3
    assert task["usage"]["fresh_tokens"] == 50
    assert task["usage"]["excluded_non_additive_rows"] == 2
    assert {item["section_id"] for item in task["work_items"]} == {
        "linked-step",
        "idless-step",
    }
    assert task["run_subgroups"] == [
        {
            "run_id": "semantic-run-one",
            "work_ids": [item["work_id"] for item in task["work_items"]],
        }
    ]
    idless_item = next(item for item in task["work_items"] if item["section_id"] == "idless-step")
    assert idless_item["client_session_id"] is None
    assert idless_item["linked_usage_records"] == 0
    idless_association = next(
        association
        for association in task["work_associations"]
        if association["work_id"] == idless_item["work_id"]
    )
    assert idless_association["basis"] == "unique_task_from_run_id"
    assert idless_association["provenance"] == ["run_id_grouping_hint"]
    assert idless_association["exact_session_id"] is None
    assert set(task["models"]) == {"gpt-root", "gpt-review", "gpt-sibling"}


# ---------------------------------------------------------------------------
# /sessions JSON shape
# ---------------------------------------------------------------------------


def test_sessions_json_shape_unchanged(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="nego-session")
    client = _client(store_root)

    json_response = client.get("/sessions")
    assert json_response.status_code == 200
    assert json_response.headers["content-type"].startswith("application/json")
    payload = json_response.json()
    assert set(payload) == {
        "schema_version",
        "session_rollup_schema_version",
        "total_sessions",
        "summary",
        "sessions",
    }
    assert payload["total_sessions"] == 1


# ---------------------------------------------------------------------------
# /health branding
# ---------------------------------------------------------------------------


def test_health_endpoint_returns_agentacct_branded_service(tmp_path):
    """#3: /health returns the agentacct-branded service string (was the
    pre-rename `agent-sentinel-local-api`, a rename miss)."""
    response = _client(tmp_path / "state").get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "agentacct-local-api"


def test_health_service_recognizer_accepts_both_brands():
    """#3: renaming the /health service string is compat-safe — the readiness
    check still accepts the pre-rename value, so a cross-version upgrade never
    falsely reports the dashboard unhealthy / not-ours. (The read-canary twin
    of this assertion was retired with canonical/read_canary.py.)"""
    from agentacct.activation import RuntimeManager

    both = {"agentacct-local-api", "agent-sentinel-local-api"}
    assert set(RuntimeManager.DASHBOARD_HEALTH_SERVICES) == both
