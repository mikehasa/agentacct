"""Phase 2 Batch C regressions: raw-tab cleanup, event labels, grouped
timeline, truncation notes, and the /sessions + /attention JSON endpoints.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

import json

from fastapi.testclient import TestClient

import agent_chronicle.api as api_module
from agent_chronicle.api import _human_event_type, create_local_api_app
from agent_chronicle.cost import CostLedger, UsageEstimate
from agent_chronicle.service import SentinelService
from agent_chronicle.work_ledger import build_work_ledger

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


def _timeline_slice(html):
    """The Work Timeline table region of the raw tab (between its header and
    the next subsection), so assertions cannot accidentally match the same
    string in another raw-tab table."""

    start = html.find("<h2>Work Timeline</h2>")
    end = html.find("Agent source discovery", start)
    assert start != -1 and end != -1
    return html[start:end]


# ---------------------------------------------------------------------------
# 2l: dead per-request computation removed
# ---------------------------------------------------------------------------


def test_dashboard_renders_without_per_request_summarize_events(tmp_path, monkeypatch):
    """The route no longer double-parses events.jsonl via summarize_events.

    Fails on pre-Batch-C code, where GET / called summarize_events(limit=500)
    and threw the result away."""

    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="sess-dead-call")

    def _boom(self, *args, **kwargs):
        raise AssertionError("summarize_events must not run for dashboard rendering")

    monkeypatch.setattr(SentinelService, "summarize_events", _boom)
    client = TestClient(create_local_api_app(store_dir=store_root))

    assert client.get("/").status_code == 200
    assert client.get("/?tab=raw").status_code == 200


def test_dead_template_helpers_removed_not_reconnected():
    """Locked decision: the dead machine-check / evidence-chain helpers are
    REMOVED (git history keeps the markup); Batch B builds fresh sections
    from Batch A ledger keys instead."""

    for name in (
        "_machine_check_rows",
        "_machine_check_status_from_exit",
        "_product_machine_check_summary",
        "_event_cost_total",
        "_is_mcp_semantic_event",
        "_event_timestamp",
    ):
        assert not hasattr(api_module, name), f"dead helper still present: {name}"
    # Reserved for Batch B: the live evidence-note helper must survive.
    assert hasattr(api_module, "_evidence_note_for_item")


# ---------------------------------------------------------------------------
# 2m: event-type labels + self-test badge + truncation notes
# ---------------------------------------------------------------------------


def test_human_event_type_covers_all_current_event_types():
    labels = {
        "model_usage": "Usage",
        "section_started": "Section started",
        "section_checkpoint": "Section checkpoint",
        "section_completed": "Section completed",
        "section_blocked": "Section blocked",
        "client_context_attached": "Client context attached",
        "agent_usage_debug_reported": "Agent usage report",
        "machine_check": "Machine check",
        "mcp_doctor_test": "Doctor test (diagnostic)",
        "workflow_smoke": "Workflow smoke (diagnostic)",
        "mcp_workflow_smoke": "Workflow smoke (diagnostic)",
        "budget_decision": "Budget decision",
        "task_started": "Task started (legacy)",
        "task_completed": "Task completed (legacy)",
        "task_blocked": "Task blocked (legacy)",
        "checkpoint": "Checkpoint",
        "note": "Note",
    }
    for event_type, expected in labels.items():
        assert _human_event_type(event_type) == expected
    assert "(diagnostic)" in _human_event_type("mcp_doctor_test")
    assert "(diagnostic)" in _human_event_type("workflow_smoke")
    # Unknown types keep the title-case fallback; empty stays generic.
    assert _human_event_type("future_event_type") == "Future Event Type"
    assert _human_event_type("") == "Activity"


def test_raw_event_log_labels_diagnostic_events_self_test(tmp_path):
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
    # event_type looks like a normal usage event) still gets the badge —
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

    raw_html = client.get("/?tab=raw").text
    product_html = client.get("/").text
    timeline = client.get("/timeline").json()["timeline"]

    assert "Doctor test (diagnostic)" in raw_html
    assert raw_html.count("self-test") >= 2
    # Diagnostics never reach the Product tab or the ledger timeline.
    assert "self-test" not in product_html
    assert "Doctor test" not in product_html
    assert timeline == []
    # The exclusion is visible, never silent.
    overview_ledger = build_work_ledger(SentinelService(store_root).list_all_events())
    assert overview_ledger["insights"]["diagnostic_tool_events"]["count"] == 2


def test_sentinel_event_log_truncation_note_only_when_truncated(tmp_path):
    store_root = tmp_path / "state"
    service = SentinelService(store_root)
    for index in range(60):
        service.record_event({"source": "codex", "event_type": "note", "metadata": {"summary": f"note {index}"}})
    client = TestClient(create_local_api_app(store_dir=store_root))

    raw_html = client.get("/?tab=raw").text

    assert "Showing 50 of 60 events." in raw_html


def test_unlinked_context_truncation_note(tmp_path):
    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root))
    for index in range(30):
        _post_section(
            client,
            section_id=f"ctx-{index}",
            title=f"Context only {index}",
            session=f"ctx-sess-{index}",
            status="started",
        )

    raw_html = client.get("/?tab=raw").text

    assert "Showing 25 of 30 unlinked context events." in raw_html


def test_budget_decisions_truncation_note(tmp_path):
    store_root = tmp_path / "state"
    ledger = CostLedger(store_root)
    for index in range(55):
        ledger.record_usage(
            UsageEstimate(
                provider="openrouter",
                model="openai/gpt-4o-mini",
                endpoint="/openrouter/v1/chat/completions",
                estimated_input_tokens=2,
                estimated_output_tokens=1,
                estimated_cost_usd=0.000001,
            ),
            run_id=f"run_budget_{index}",
            decision="allowed",
        )
    client = TestClient(create_local_api_app(store_dir=store_root))

    raw_html = client.get("/?tab=raw").text

    assert "Showing 50 of 55 budget decisions." in raw_html


def test_no_truncation_notes_below_caps_and_static_commands_note(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="sess-small")
    service = SentinelService(store_root)
    service.record_event({"source": "codex", "event_type": "note", "metadata": {"summary": "one note"}})
    client = TestClient(create_local_api_app(store_dir=store_root))

    product_html = client.get("/").text
    raw_html = client.get("/?tab=raw").text

    assert "Showing" not in product_html
    assert "Showing" not in raw_html
    assert "Latest 20 commands." in raw_html


# ---------------------------------------------------------------------------
# 2n: grouped Work Timeline default + views
# ---------------------------------------------------------------------------


def _seed_drowning_store(tmp_path):
    """3 sections + 1 machine check recorded BEFORE 100 usage rows, so the
    raw row-level mix (capped at 80) drowns every MCP entry — the exact
    regression class from the readiness review."""

    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root))
    for name in ("alpha", "beta", "gamma"):
        _post_section(
            client,
            section_id=f"flood-{name}",
            title=f"Flood section {name}",
            session=f"mcp-sess-{name}",
        )
    check = client.post(
        "/events",
        json={
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {"name": "pytest", "exit_code": 0, "summary": "Flood machine check evidence summary."},
        },
    )
    assert check.status_code == 200
    for index in range(100):
        _trusted_usage(store_root, session=f"flood-sess-{index:03d}")
    return store_root, client


def test_grouped_timeline_default_keeps_sections_visible(tmp_path):
    _store_root, client = _seed_drowning_store(tmp_path)

    raw_html = client.get("/?tab=raw").text
    timeline_html = _timeline_slice(raw_html)

    # All MCP entries visible on page one despite 100 usage rows.
    assert "Flood section alpha" in timeline_html
    assert "Flood section beta" in timeline_html
    assert "Flood section gamma" in timeline_html
    assert "Flood machine check evidence summary." in timeline_html
    # One synthetic group row with exact counts and sums, fresh-first.
    assert "Codex usage — 100 rows" in timeline_html
    assert "100 rows" in timeline_html
    assert "1,500 fresh" in timeline_html
    assert "$1.00" in timeline_html
    assert "grouped by client/day" in timeline_html
    # Group rows never leak session ids.
    assert "flood-sess-" not in timeline_html
    # Nothing truncated in the grouped view: no cap note for the timeline.
    assert "timeline entries." not in raw_html

    # The old default is one click away and pins the drowned behavior.
    all_html = client.get("/?tab=raw&timeline_view=all").text
    all_timeline = _timeline_slice(all_html)
    assert "Showing 80 of 104 timeline entries." in all_html
    assert "Flood section alpha" not in all_timeline
    assert "Codex usage — 100 rows" not in all_timeline


def test_timeline_view_param_filters_and_falls_back(tmp_path):
    _store_root, client = _seed_drowning_store(tmp_path)

    mcp_html = client.get("/?tab=raw&timeline_view=mcp").text
    mcp_timeline = _timeline_slice(mcp_html)
    assert "Flood section alpha" in mcp_timeline
    assert "Flood machine check evidence summary." in mcp_timeline
    assert "codex usage" not in mcp_timeline
    assert "Codex usage —" not in mcp_timeline

    usage_html = client.get("/?tab=raw&timeline_view=usage").text
    usage_timeline = _timeline_slice(usage_html)
    assert "Flood section alpha" not in usage_timeline
    assert "codex usage" in usage_timeline
    assert "Showing 80 of 100 timeline entries." in usage_html

    # Invalid value falls back to the grouped default (_safe_choice).
    bogus_html = client.get("/?tab=raw&timeline_view=bogus").text
    assert "Codex usage — 100 rows" in _timeline_slice(bogus_html)

    # Sort/tab links preserve the active view.
    assert "timeline_view=mcp" in mcp_html
    assert "timeline_view=all" in mcp_html  # the Everything toggle itself


def test_grouped_timeline_display_honesty_counts_only(tmp_path):
    """A grouped row shows attributed vs not-attributed COUNTS that match the
    canonical ledger and never carries a work title or session id — the view
    cannot imply an allocation the ledger did not make."""

    store_root = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_root))
    _post_section(client, section_id="honest-work", title="Honest attribution target", session="sess-attr")
    _trusted_usage(store_root, session="sess-attr")
    for name in ("b", "c", "d", "e"):
        _trusted_usage(store_root, session=f"sess-{name}")

    overview = client.get("/overview").json()["overview"]
    assert overview["attributed_usage_count"] == 1

    raw_html = client.get("/?tab=raw").text
    timeline_html = _timeline_slice(raw_html)
    join_label = "1 attributed / 4 not attributed"
    assert join_label in timeline_html

    # Isolate the group <tr> and prove it carries no work title / session id.
    label_at = timeline_html.find(join_label)
    row_start = timeline_html.rfind("<tr>", 0, label_at)
    row_end = timeline_html.find("</tr>", label_at)
    group_row = timeline_html[row_start:row_end]
    assert "Honest attribution target" not in group_row
    assert "sess-attr" not in group_row
    assert "Codex usage — 5 rows" in group_row


# ---------------------------------------------------------------------------
# 2o: JSON endpoints
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
