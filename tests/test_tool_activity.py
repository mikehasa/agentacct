"""Privacy-safe tool-category capture for the Receipt's Actions dimension."""

from __future__ import annotations

import json
from pathlib import Path

from agentacct.hooks import capture_tool_activity
from agentacct.tool_activity import (
    TOOL_ACTIVITY_EVENT_TYPE,
    TOOL_CATEGORIES,
    build_tool_activity_by_session,
    build_tool_names_by_session,
    drain_tool_activity_spool,
    normalize_tool_name,
    record_tool_activity_tick,
    tool_activity_spool_path,
    tool_category,
)


def test_normalize_tool_name_trims_bounds_and_rejects_blank() -> None:
    assert normalize_tool_name("  Read  ") == "Read"
    assert normalize_tool_name("mcp__acme__deploy") == "mcp__acme__deploy"
    assert normalize_tool_name("") is None
    assert normalize_tool_name(None) is None
    assert normalize_tool_name("   ") is None
    assert len(normalize_tool_name("x" * 500)) == 120  # bounded, never unbounded


def test_tool_name_capture_round_trips_through_spool_and_drain(tmp_path: Path) -> None:
    # Ticks WITH names aggregate into per-session name counts; a name-less tick
    # (older format) still counts toward the category, an honest name undercount.
    names = ["Bash", "Bash", "Read", "mcp__agentacct__record_section"]
    for i, name in enumerate(names):
        record_tool_activity_tick(
            tmp_path, client="claude-code", session_id="s1",
            category=tool_category(name), name=name, at=100.0 + i,
        )
    record_tool_activity_tick(
        tmp_path, client="claude-code", session_id="s1", category="search", at=200.0
    )  # no name

    [event] = drain_tool_activity_spool(tmp_path, now=300.0, token="t")
    # Names ride as list VALUES (not dict keys) so credential-shaped connector
    # names survive the store's secret redaction.
    got = {entry["name"]: entry["count"] for entry in event["metadata"]["tool_names"]}
    assert got == {"Bash": 2, "Read": 1, "mcp__agentacct__record_section": 1}
    # The name-less tick is counted as a category but contributes no name.
    assert event["metadata"]["tool_category_counts"] == {"execute": 2, "read": 1, "mcp": 1, "search": 1}


def test_build_tool_names_by_session_sums_additive_batches() -> None:
    def batch(session: str, names: dict) -> dict:
        return {
            "event_type": TOOL_ACTIVITY_EVENT_TYPE,
            "metadata": {
                "client": "claude-code",
                "client_session_id": session,
                "tool_names": [{"name": n, "count": c} for n, c in names.items()],
            },
        }

    result = build_tool_names_by_session(
        [
            batch("s1", {"Bash": 2, "Read": 1}),
            batch("s1", {"Bash": 3}),
            # non-positive / boolean counts ignored; a batch with no names contributes nothing
            batch("s1", {"Read": -1, "Edit": True}),
            batch("s2", {"mcp__acme__deploy": 4}),
            {"event_type": "model_usage", "metadata": {"client": "x", "client_session_id": "s1"}},
        ]
    )
    assert result[("claude-code", "s1")] == {"Bash": 5, "Read": 1}
    assert result[("claude-code", "s2")] == {"mcp__acme__deploy": 4}


def test_tool_category_maps_names_only_and_collapses_mcp() -> None:
    assert tool_category("Read") == "read"
    assert tool_category("MultiEdit") == "edit"
    assert tool_category("Bash") == "execute"
    assert tool_category("Grep") == "search"
    assert tool_category("WebFetch") == "network"
    assert tool_category("Task") == "agent"
    assert tool_category("TodoWrite") == "plan"
    # Names are matched case-insensitively.
    assert tool_category("READ_FILE") == "read"
    # opencode names not shared with the Claude/Codex tools.
    assert tool_category("list") == "search"
    assert tool_category("patch") == "edit"
    assert tool_category("todoread") == "plan"
    # Anthropic's text-editor tool, whatever the API version names it.
    assert tool_category("str_replace_based_edit_tool") == "edit"
    assert tool_category("str_replace_editor") == "edit"
    assert tool_category("text_editor") == "edit"
    # Cross-agent snake_case names (Cursor/Windsurf/MCP) that used to be `other`.
    assert tool_category("read_file") == "read"
    assert tool_category("write_file") == "edit"
    assert tool_category("edit_file") == "edit"
    assert tool_category("list_dir") == "search"
    assert tool_category("codebase_search") == "search"
    assert tool_category("run_terminal_cmd") == "execute"
    assert tool_category("web_search") == "network"
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


def test_capture_tool_activity_records_name_and_category_but_never_arguments(tmp_path: Path) -> None:
    raw = json.dumps(
        {"session_id": "hooksess", "tool_name": "Edit", "tool_input": {"file_path": "/secret/path.py"}}
    )
    capture_tool_activity(raw, store_dir=tmp_path)
    # The captured tick carries the tool NAME and its category — but never the
    # arguments/path (``tool_input``): "secret" must not land, "Edit" now does.
    spool_text = tool_activity_spool_path(tmp_path).read_text(encoding="utf-8")
    assert "secret" not in spool_text
    assert '"n":"Edit"' in spool_text
    [event] = drain_tool_activity_spool(tmp_path, now=1.0, token="t")
    assert event["metadata"]["tool_category_counts"] == {"edit": 1}
    assert event["metadata"]["tool_names"] == [{"name": "Edit", "count": 1}]
    assert event["metadata"]["client"] == "claude-code"


def test_capture_tool_activity_without_session_is_a_noop(tmp_path: Path) -> None:
    capture_tool_activity(json.dumps({"tool_name": "Read"}), store_dir=tmp_path)
    assert not tool_activity_spool_path(tmp_path).exists()
