"""Privacy-safe tool-category capture for the Receipt's Actions dimension."""

from __future__ import annotations

import json
from pathlib import Path

from agentacct.hooks import capture_tool_activity
from agentacct.tool_activity import (
    TOOL_ACTIVITY_EVENT_TYPE,
    TOOL_CATEGORIES,
    build_tool_activity_by_session,
    drain_tool_activity_spool,
    record_tool_activity_tick,
    tool_activity_spool_path,
    tool_category,
)


def test_tool_category_maps_names_only_and_collapses_mcp() -> None:
    assert tool_category("Read") == "read"
    assert tool_category("MultiEdit") == "edit"
    assert tool_category("Bash") == "execute"
    assert tool_category("Grep") == "search"
    assert tool_category("WebFetch") == "network"
    assert tool_category("Task") == "agent"
    assert tool_category("TodoWrite") == "plan"
    # Any MCP tool collapses to a single bucket by PREFIX, so a specific MCP
    # tool name (which could reveal a connector) is never recorded.
    assert tool_category("mcp__github__create_issue") == "mcp"
    assert tool_category("mcp__anything__at__all") == "mcp"
    # Unknown names are bucketed honestly, never dropped.
    assert tool_category("SomeFutureTool") == "other"
    assert tool_category("") == "other"
    assert tool_category(None) == "other"
    # Every produced category is in the closed set.
    for name in ("Read", "Bash", "mcp__x__y", "Unknown", ""):
        assert tool_category(name) in TOOL_CATEGORIES


def test_spool_tick_and_drain_roundtrip(tmp_path: Path) -> None:
    for category in ("read", "read", "edit", "execute"):
        record_tool_activity_tick(
            tmp_path, client="claude-code", session_id="sess-1", category=category, at=100.0
        )
    events = drain_tool_activity_spool(tmp_path, now=200.0, token="t1")
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == TOOL_ACTIVITY_EVENT_TYPE
    assert event["metadata"]["client"] == "claude-code"
    assert event["metadata"]["client_session_id"] == "sess-1"
    assert event["metadata"]["tool_category_counts"] == {"read": 2, "edit": 1, "execute": 1}
    # The spool is consumed: a second drain sees nothing.
    assert drain_tool_activity_spool(tmp_path, now=300.0, token="t2") == []


def test_drained_event_never_carries_tool_names_or_arguments(tmp_path: Path) -> None:
    record_tool_activity_tick(
        tmp_path, client="claude-code", session_id="s", category="execute", at=1.0
    )
    # The spool bytes themselves must not contain a tool name or command.
    spool_text = tool_activity_spool_path(tmp_path).read_text(encoding="utf-8")
    assert "Bash" not in spool_text and "tool_name" not in spool_text
    assert "execute" in spool_text
    [event] = drain_tool_activity_spool(tmp_path, now=2.0, token="t")
    allowed = {
        "client",
        "client_session_id",
        "tool_category_counts",
        "capture_basis",
        "captured_at",
        "sentinel_semantic_kind",
    }
    assert set(event["metadata"]) <= allowed
    blob = json.dumps(event)
    for forbidden in ("tool_name", "tool_input", "command", "arguments", "args"):
        assert forbidden not in blob


def test_build_tool_activity_by_session_sums_batches_and_rejects_junk() -> None:
    def batch(session: str, counts: dict) -> dict:
        return {
            "event_type": TOOL_ACTIVITY_EVENT_TYPE,
            "metadata": {
                "client": "claude-code",
                "client_session_id": session,
                "tool_category_counts": counts,
            },
        }

    result = build_tool_activity_by_session(
        [
            batch("s1", {"read": 2, "edit": 1}),
            batch("s1", {"read": 3, "search": 5}),
            batch("s2", {"execute": 4}),
            # Unknown categories and non-positive/boolean counts are ignored.
            batch("s1", {"not_a_category": 9, "edit": -1, "read": True}),
            # Non-activity events are ignored.
            {"event_type": "model_usage", "metadata": {"client": "x", "client_session_id": "s1"}},
        ]
    )
    assert result[("claude-code", "s1")] == {"read": 5, "edit": 1, "search": 5}
    assert result[("claude-code", "s2")] == {"execute": 4}


def test_capture_tool_activity_records_category_from_raw_event(tmp_path: Path) -> None:
    raw = json.dumps(
        {"session_id": "hooksess", "tool_name": "Edit", "tool_input": {"file_path": "/secret/path.py"}}
    )
    capture_tool_activity(raw, store_dir=tmp_path)
    # The captured tick is a category only; the file path/tool_input never lands.
    spool_text = tool_activity_spool_path(tmp_path).read_text(encoding="utf-8")
    assert "secret" not in spool_text and "Edit" not in spool_text
    [event] = drain_tool_activity_spool(tmp_path, now=1.0, token="t")
    assert event["metadata"]["tool_category_counts"] == {"edit": 1}
    assert event["metadata"]["client"] == "claude-code"


def test_capture_tool_activity_without_session_is_a_noop(tmp_path: Path) -> None:
    capture_tool_activity(json.dumps({"tool_name": "Read"}), store_dir=tmp_path)
    assert not tool_activity_spool_path(tmp_path).exists()
