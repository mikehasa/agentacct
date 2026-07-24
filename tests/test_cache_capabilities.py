from __future__ import annotations

from agent_chronicle.api import DashboardUsageRecord, _usage_record_time
from agent_chronicle.task_projection import build_task_projection
from agent_chronicle.usage_cube import build_usage_cube
from agent_chronicle.work_ledger import build_work_ledger


def _trusted_usage(
    *,
    event_id: str,
    session: str,
    parent: str | None = None,
    cache_write: int = 0,
    cache_read: int = 0,
    cache_write_reported: bool,
    cache_read_reported: bool,
) -> dict:
    return {
        "event_id": event_id,
        "created_at": 100.0,
        "source": "codex-local-session-import",
        "event_type": "model_usage",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 20,
        "estimated_cost_usd": None,
        "usage_confidence": "client_reported",
        "cost_confidence": "unknown",
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": "codex",
            "client_session_id": session,
            "client_session_kind": "child" if parent else "root",
            "parent_client_session_id": parent,
            "cached_input_tokens": cache_write + cache_read,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
            "cache_creation_tokens_reported": cache_write_reported,
            "cache_read_tokens_reported": cache_read_reported,
        },
    }


def test_row_capabilities_survive_work_ledger_and_task_projection() -> None:
    ledger = build_work_ledger(
        [
            _trusted_usage(
                event_id="usage-root",
                session="root",
                cache_read=90,
                cache_write_reported=False,
                cache_read_reported=True,
            ),
            _trusted_usage(
                event_id="usage-child",
                session="child",
                parent="root",
                cache_write=30,
                cache_read=70,
                cache_write_reported=True,
                cache_read_reported=True,
            ),
        ]
    )

    sessions = ledger["session_rollup"]["sessions"]
    by_id = {entry["client_session_id"]: entry for entry in sessions}
    assert by_id["root"]["usage"]["cache_creation_reporting"] == "not_reported"
    assert by_id["root"]["usage"]["cache_read_reporting"] == "reported"
    # The child row remains visible, but its cumulative counters and their
    # capability split stay out of additive Task totals until normalization.
    assert by_id["child"]["usage"]["cache_creation_reporting"] is None
    assert by_id["child"]["usage"]["cache_read_reporting"] is None
    assert by_id["child"]["usage"]["excluded_non_additive_rows"] == 1

    task = build_task_projection(sessions, [])["tasks"][0]
    assert task["usage"]["cache_creation_tokens"] == 0
    assert task["usage"]["cache_creation_reporting"] == "not_reported"
    assert task["usage"]["cache_creation_reported_rows"] == 0
    assert task["usage"]["cache_creation_unreported_rows"] == 1
    assert task["usage"]["cache_read_tokens"] == 90
    assert task["usage"]["cache_read_reporting"] == "reported"
    assert task["usage"]["excluded_non_additive_rows"] == 1


def test_usage_cube_keeps_zero_separate_from_not_reported() -> None:
    records = [
        DashboardUsageRecord(
            client="codex",
            provider="codex",
            model="gpt-5.6-sol",
            session_id="unreported",
            session_kind="root",
            input_tokens=10,
            output_tokens=2,
            cached_input_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=5,
            cache_creation_tokens_reported=False,
            cache_read_tokens_reported=True,
            updated_at=100.0,
        ),
        DashboardUsageRecord(
            client="claude-code",
            provider="anthropic",
            model="claude-opus",
            session_id="reported-zero",
            session_kind="root",
            input_tokens=10,
            output_tokens=2,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_tokens_reported=True,
            cache_read_tokens_reported=True,
            updated_at=100.0,
        ),
    ]

    cube = build_usage_cube(
        records,
        record_time=_usage_record_time,
        days=None,
        granularity="daily",
    )

    assert cube["totals"]["cache_creation_tokens"] == 0
    assert cube["totals"]["cache_creation_reporting"] == "partial"
    assert cube["totals"]["cache_read_reporting"] == "reported"
    by_client = {entry["client"]: entry for entry in cube["by_client"]}
    assert by_client["codex"]["cache_creation_reporting"] == "not_reported"
    assert by_client["claude-code"]["cache_creation_reporting"] == "reported"


def test_missing_capability_flags_remain_unknown_not_reported_zero() -> None:
    record = DashboardUsageRecord(
        client="legacy-client",
        provider="legacy-provider",
        model="legacy-model",
        session_id="legacy-row",
        session_kind="root",
        input_tokens=10,
        output_tokens=2,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        updated_at=100.0,
    )

    cube = build_usage_cube(
        [record],
        record_time=_usage_record_time,
        days=None,
        granularity="daily",
    )

    assert cube["totals"]["cache_creation_tokens"] == 0
    assert cube["totals"]["cache_creation_reporting"] == "unknown"
    assert cube["totals"]["cache_creation_unknown_rows"] == 1
    assert cube["totals"]["cache_read_tokens"] == 0
    assert cube["totals"]["cache_read_reporting"] == "unknown"
    assert cube["totals"]["cache_read_unknown_rows"] == 1
