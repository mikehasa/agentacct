from __future__ import annotations

import json

from agentacct.evidence_runtime import EVIDENCE_V2_ENV, EvidenceRuntime, evidence_v2_enabled
from agentacct.service import SentinelService


def _v1_event() -> dict[str, object]:
    return {
        "event_id": "evt-work-1",
        "created_at": 1_750_000_000.0,
        "source": "codex",
        "event_type": "section_completed",
        "run_id": "run-1",
        "metadata": {
            "sentinel_semantic_kind": "section",
            "section_id": "implementation",
            "client": "codex",
            "client_session_id": "session-1",
            "summary": "Evidence core implemented.",
            "prompt": "PROMPT_CANARY",
            "tool_result": "TOOL_RESULT_CANARY",
        },
    }


def test_shadow_v1_is_idempotent_and_records_transport(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)

    first = runtime.shadow_v1_event(_v1_event(), transport="mcp")
    second = runtime.shadow_v1_event(_v1_event(), transport="mcp")

    assert first.disposition == "inserted"
    assert second.disposition == "duplicate"
    assert first.evidence_id == second.evidence_id
    envelope = runtime.envelopes()[0]
    assert envelope.source_instance == "v1-mcp"
    assert envelope.assertion == "claimed"
    assert envelope.subjects.client_session_id == "session-1"
    encoded = json.dumps(envelope.to_dict(), sort_keys=True)
    assert "PROMPT_CANARY" not in encoded
    assert "TOOL_RESULT_CANARY" not in encoded


def test_shadow_failure_is_fail_open(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    malformed = {**_v1_event(), "created_at": 1e400}

    result = runtime.shadow_v1_event(malformed, transport="mcp")

    assert result.enabled is True
    assert result.appended is False
    assert result.error


def test_disabled_runtime_does_not_create_v2_store(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=False)

    result = runtime.shadow_v1_event(_v1_event(), transport="mcp")

    assert result.enabled is False
    assert not (tmp_path / "evidence-v2").exists()
    assert runtime.product()["status"]["enabled"] is False
    assert runtime.dashboard_product(limit=2000)["status"] == {
        "enabled": False,
        "reason": f"disabled by {EVIDENCE_V2_ENV}",
        "scope": "advanced_html_preview",
        "limit": 2000,
        "truncated": False,
        "store_stats_included": False,
    }


def test_product_reads_stats_once_and_skips_empty_conflict_scan(tmp_path, monkeypatch) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    runtime.shadow_v1_event(_v1_event(), transport="mcp")
    store = runtime.store
    original_stats = store.stats
    stats_calls = 0

    def counted_stats():
        nonlocal stats_calls
        stats_calls += 1
        return original_stats()

    def unexpected_conflict_scan():
        raise AssertionError("conflict rows must not be scanned when exact stats report none")

    monkeypatch.setattr(store, "stats", counted_stats)
    monkeypatch.setattr(runtime, "_conflict_groups", unexpected_conflict_scan)

    product = runtime.product()

    assert product["status"]["stats"]["conflict_versions"] == 0
    assert stats_calls == 1


def test_dashboard_product_is_recent_bounded_and_skips_full_store_stats(tmp_path, monkeypatch) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    for index in range(3):
        event = _v1_event()
        event["event_id"] = f"evt-dashboard-{index}"
        event["created_at"] = 1_750_000_000.0 + index
        runtime.shadow_v1_event(event, transport="mcp")
    store = runtime.store
    original_query = store.query
    query_calls = []

    def counted_query(**kwargs):
        query_calls.append(kwargs)
        return original_query(**kwargs)

    def unexpected_full_store_read(*args, **kwargs):
        raise AssertionError("the Advanced HTML preview must not scan exact full-store counters or conflicts")

    monkeypatch.setattr(store, "query", counted_query)
    monkeypatch.setattr(store, "stats", unexpected_full_store_read)
    monkeypatch.setattr(runtime, "_conflict_groups", unexpected_full_store_read)

    product = runtime.dashboard_product(limit=2)

    assert product["summary"]["evidence_count"] == 2
    assert product["status"] == {
        "enabled": True,
        "store": "evidence-v2",
        "scope": "advanced_html_preview",
        "selection": "latest_event_time",
        "limit": 2,
        "truncated": True,
        "store_stats_included": False,
    }
    assert query_calls == [{"limit": 3, "order_by": "event_time", "descending": True}]


def test_disabled_service_keeps_v1_recording_unchanged_and_creates_no_v2_store(tmp_path) -> None:
    service = SentinelService(tmp_path, evidence_v2_enabled=False)

    recorded = service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {"section_id": "rollback-check"},
        },
        transport="mcp",
    )

    assert service.list_all_events() == [recorded]
    assert service.events_path.is_file()
    assert not (tmp_path / "evidence-v2").exists()


def test_environment_flag_defaults_on_and_can_disable(monkeypatch) -> None:
    monkeypatch.delenv(EVIDENCE_V2_ENV, raising=False)
    monkeypatch.delenv("AGENT_SENTINEL_EVIDENCE_V2", raising=False)
    assert evidence_v2_enabled() is True

    monkeypatch.setenv(EVIDENCE_V2_ENV, "off")
    assert evidence_v2_enabled() is False
