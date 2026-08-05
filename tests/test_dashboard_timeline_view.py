"""JSON data-lane regressions: diagnostic-event exclusion, overview
attribution counts, and the /sessions + /attention JSON endpoints.

(The HTML raw-tab/timeline rendering tests that used to live here were
removed with the HTML display layer.)

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

import json

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.service import SentinelService
from agentacct.work_ledger import build_work_ledger

LEDGER_SCHEMA_V2 = "agent-sentinel.work-ledger.v2"


def _trusted_usage(store_root, *, session, client="codex", input_tokens=10, output_tokens=5, cost=0.01):
    return SentinelService(store_root).record_event(
        {
            "source": f"{client}-local-session-import",
            "event_type": "model_usage",
            "provider": client,
            "model": "gpt-5.5",
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_cost_usd": cost,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": client,
                "client_session_id": session,
                "cached_input_tokens": 0,
            },
        },
        trusted_usage_import=True,
    )


def _post_section(client, *, section_id, title, session, source="codex", status="completed"):
    response = client.post(
        "/events",
        json={
            "source": source,
            "event_type": f"section_{status}" if status in {"started", "completed", "blocked"} else "section_checkpoint",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": section_id,
                "section_status": status,
                "section_title": title,
                "client": source,
                "client_session_id": session,
            },
        },
    )
    assert response.status_code == 200
    return response


# ---------------------------------------------------------------------------
# Diagnostic events stay out of the ledger timeline, visibly counted
# ---------------------------------------------------------------------------


def test_diagnostic_events_excluded_from_timeline_but_counted(tmp_path):
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    service.record_event(
        {
            "source": "agent-sentinel-mcp-doctor",
            "event_type": "mcp_doctor_test",
            "metadata": {"note": "doctor probe"},
        }
    )
    # Forward-looking guarantee: a diagnostic recognized by SOURCE only (its
    # event_type looks like a normal usage event) is still excluded —
    # usage_truth.is_diagnostic_event is the one rule, never re-derived.
    service.record_event(
        {
            "source": "agent-sentinel-mcp-workflow-smoke",
            "event_type": "model_usage",
            "estimated_input_tokens": 3,
            "estimated_output_tokens": 2,
        }
    )
    client = TestClient(create_local_api_app(store_dir=store_root))

    timeline = client.get("/timeline").json()["timeline"]

    # Diagnostics never reach the ledger timeline.
    assert timeline == []
    # The exclusion is visible, never silent.
    overview_ledger = build_work_ledger(SentinelService(store_root).list_all_events())
    assert overview_ledger["insights"]["diagnostic_tool_events"]["count"] == 2


# ---------------------------------------------------------------------------
# Overview attribution honesty: counts match the canonical ledger
# ---------------------------------------------------------------------------


def test_overview_attributed_usage_count_matches_ledger(tmp_path):
    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root))
    _post_section(client, section_id="honest-work", title="Honest attribution target", session="sess-attr")
    _trusted_usage(store_root, session="sess-attr")
    for name in ("b", "c", "d", "e"):
        _trusted_usage(store_root, session=f"sess-{name}")

    overview = client.get("/overview").json()["overview"]

    assert overview["attributed_usage_count"] == 1


# ---------------------------------------------------------------------------
# JSON endpoints
# ---------------------------------------------------------------------------


def test_sessions_endpoint_serves_rollup_verbatim_with_limit(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="sess-a")
    _trusted_usage(store_root, session="sess-b")
    _trusted_usage(store_root, session="sess-c")
    client = TestClient(create_local_api_app(store_dir=store_root))
    _post_section(client, section_id="rollup-work", title="Rollup work", session="sess-a")

    payload = client.get("/sessions").json()

    expected = build_work_ledger(SentinelService(store_root).list_all_events(), run_reports=[], cost_events=[])
    expected_rollup = json.loads(json.dumps(expected["session_rollup"]))
    assert payload["schema_version"] == LEDGER_SCHEMA_V2
    assert payload["session_rollup_schema_version"] == "agent-sentinel.session-rollup.v1"
    assert payload["total_sessions"] == 3
    assert payload["sessions"] == expected_rollup["sessions"]
    assert payload["summary"] == expected_rollup["summary"]
    # Full ids stay JSON-only alongside the short display label.
    assert payload["sessions"][0]["client_session_id"]
    assert payload["sessions"][0]["client_session_id_short"]

    limited = client.get("/sessions?limit=1").json()
    assert len(limited["sessions"]) == 1
    assert limited["sessions"][0] == expected_rollup["sessions"][0]
    # total_sessions and summary always describe the FULL store.
    assert limited["total_sessions"] == 3
    assert limited["summary"] == expected_rollup["summary"]

    assert client.get("/sessions?limit=0").status_code == 422
    assert client.get("/sessions?limit=1001").status_code == 422
    assert client.post("/sessions").status_code == 405


def test_attention_endpoint_serves_groups_verbatim(tmp_path):
    store_root = tmp_path / "state"
    for name in ("a", "b", "c"):
        _trusted_usage(store_root, session=f"attn-sess-{name}")
    client = TestClient(create_local_api_app(store_dir=store_root))

    payload = client.get("/attention").json()

    expected = build_work_ledger(SentinelService(store_root).list_all_events(), run_reports=[], cost_events=[])
    expected_groups = json.loads(json.dumps(expected["attention_groups"]))
    assert payload["schema_version"] == LEDGER_SCHEMA_V2
    assert payload["attention_groups"] == expected_groups["groups"]
    assert payload["total_items"] == expected_groups["total_items"]
    # Grouping semantics: one group per cause, counts sum to total_items.
    assert payload["total_items"] == sum(group["count"] for group in payload["attention_groups"])
    causes = {group["cause"] for group in payload["attention_groups"]}
    assert "usage_truth_without_mcp_context" in causes
    assert client.post("/attention").status_code == 405


def test_ledger_json_envelopes_carry_schema_version_v2(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="sess-schema")
    client = TestClient(create_local_api_app(store_dir=store_root))
    _post_section(client, section_id="schema-work", title="Schema work", session="sess-schema")

    assert client.get("/overview").json()["schema_version"] == LEDGER_SCHEMA_V2
    assert client.get("/timeline").json()["schema_version"] == LEDGER_SCHEMA_V2
    work_items_payload = client.get("/work-items").json()
    assert work_items_payload["schema_version"] == LEDGER_SCHEMA_V2
    work_id = work_items_payload["work_items"][0]["work_id"]
    item_payload = client.get(f"/work-items/{work_id}").json()
    assert item_payload["schema_version"] == LEDGER_SCHEMA_V2
    assert item_payload["work_item"]["work_id"] == work_id
