"""Discovery-side Codex Actions extraction (commands / touched files / tools).

Codex's PreToolUse hook barely fires for its built-in tools, so these signals
are derived from the rollout on disk at discovery time instead. The rollout is
the same file the token importer already reads.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agentacct import client_usage as cu
from agentacct.client_usage import discover_codex_usage
from agentacct.tool_activity import (
    DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS,
    TOOL_ACTIVITY_EVENT_TYPE,
    build_commands_by_session,
    build_discovery_tool_activity_event,
    build_tool_activity_by_session,
    build_tool_names_by_session,
    build_touched_files_by_session,
    is_discovery_tool_activity_event,
)

from tests.test_client_usage import _make_codex_home, _add_codex_thread


# --- pure extraction helpers ------------------------------------------------


def _tool_activity_from_calls(
    calls: list[tuple[str, object]],
    *,
    cwd: str | None,
) -> dict[str, object]:
    accumulator = cu._CodexToolActivityAccumulator()
    identified_calls = [
        (f"call-{index}", tool_name, arguments)
        for index, (tool_name, arguments) in enumerate(calls)
    ]
    _record_calls_and_touched_paths(accumulator, identified_calls, cwd=cwd)
    return accumulator.as_activity()


def _record_calls_and_touched_paths(
    accumulator: cu._CodexToolActivityAccumulator,
    calls: list[tuple[object, object, object]],
    *,
    cwd: str | None,
) -> None:
    """Run the accumulator's identity and path phases over the same carriers."""

    for source_line_number, (call_id, tool_name, arguments) in enumerate(
        calls,
        start=1,
    ):
        accumulator.record_call(
            call_id,
            tool_name,
            arguments,
            source_line_number=source_line_number,
        )
    for source_line_number, (call_id, tool_name, arguments) in enumerate(
        calls,
        start=1,
    ):
        if accumulator.expects_carrier_on_source_line(source_line_number):
            accumulator.record_touched_paths(
                source_line_number,
                cu._CodexActionCarrier(
                    call_id=call_id,
                    action_name=tool_name,
                    raw_arguments=arguments,
                ),
                cwd=cwd,
            )
    accumulator.finish_touched_path_scan(source_unchanged=True)


def test_apply_patch_paths_parses_add_update_delete_move():
    patch = (
        "*** Begin Patch\n"
        "*** Add File: src/new.py\n+print('x')\n"
        "*** Update File: src/old.py\n@@\n-a\n+b\n"
        "*** Delete File: docs/gone.md\n"
        "*** Move to: src/renamed.py\n"
        "*** End Patch\n"
    )
    assert cu._codex_apply_patch_paths(patch) == [
        "src/new.py",
        "src/old.py",
        "docs/gone.md",
        "src/renamed.py",
    ]


def test_apply_patch_paths_ignores_non_patch_text():
    assert cu._codex_apply_patch_paths("just some output, no patch") == []
    assert cu._codex_apply_patch_paths(None) == []


def test_command_text_from_exec_command_json():
    raw = json.dumps({"cmd": "pytest -q", "workdir": "/repo", "yield_time_ms": 10})
    assert cu._codex_command_text("exec_command", raw) == "pytest -q"


def test_command_text_from_js_exec_handles_escaped_quotes():
    js = 'const r = await tools.exec_command({cmd:"echo \\"hi there\\"",workdir:"/r"});text(r.output);'
    # The embedded escaped quote must NOT truncate the command mid-string.
    assert cu._codex_command_text("exec", js) == 'echo \\"hi there\\"'


def test_command_text_from_js_exec_single_quoted():
    js = "await tools.exec_command({cmd:'ls -la', workdir:'/r'});"
    assert cu._codex_command_text("exec", js) == "ls -la"


def test_command_text_skips_unsupported_arguments():
    # A backtick/template cmd is deliberately skipped (honest undercount) rather
    # than captured half-parsed.
    assert cu._codex_command_text("exec", "tools.exec_command({cmd:`git ${x}`})") is None
    assert cu._codex_command_text("exec_command", "not json at all") is None
    assert cu._codex_command_text("exec_command", "") is None
    deeply_nested_json = "[" * 10_000 + "0" + "]" * 10_000
    assert cu._codex_command_text("exec_command", deeply_nested_json) is None


def test_relativize_touched_path_keeps_relative_relativizes_under_cwd_drops_outside():
    assert cu._codex_relativize_touched_path("src/a.py", "/work/project") == "src/a.py"
    assert (
        cu._codex_relativize_touched_path("/work/project/src/a.py", "/work/project")
        == "src/a.py"
    )
    # Absolute path outside cwd carries a home/username-leaking prefix -> dropped.
    assert cu._codex_relativize_touched_path("/etc/passwd", "/work/project") is None
    # Absolute path with no absolute cwd to relativize against -> dropped.
    assert cu._codex_relativize_touched_path("/abs/x", None) is None


def test_tool_activity_from_calls_aggregates_all_signals():
    calls = [
        ("exec", 'tools.exec_command({cmd:"git status"})'),
        ("exec_command", json.dumps({"cmd": "pytest -q"})),
        ("apply_patch", "*** Begin Patch\n*** Add File: src/a.py\n+x\n*** End Patch\n"),
        ("spawn_agent", "{}"),
        ("some_unknown_tool", "{}"),  # unknown -> category "other", name kept
    ]
    activity = _tool_activity_from_calls(calls, cwd="/work/project")
    assert activity["tool_category_counts"] == {
        "agent": 1,
        "edit": 1,
        "execute": 2,
        "other": 1,
    }
    names = {entry["name"]: entry["count"] for entry in activity["tool_names"]}
    assert names == {
        "exec": 1,
        "exec_command": 1,
        "apply_patch": 1,
        "spawn_agent": 1,
        "some_unknown_tool": 1,
    }
    assert activity["commands"] == ["git status", "pytest -q"]
    assert activity["touched_files"] == ["src/a.py"]


def test_tool_activity_from_calls_dedupes_commands_and_paths():
    calls = [
        ("exec_command", json.dumps({"cmd": "ls"})),
        ("exec_command", json.dumps({"cmd": "ls"})),
        ("apply_patch", "*** Add File: a.py\n"),
        ("apply_patch", "*** Update File: a.py\n"),
    ]
    activity = _tool_activity_from_calls(calls, cwd=None)
    assert activity["commands"] == ["ls"]
    assert activity["touched_files"] == ["a.py"]


def test_touched_path_cap_counts_unique_paths_across_calls():
    accumulator = cu._CodexToolActivityAccumulator(touched_path_cap=2)
    calls = [
        (call_id, "apply_patch", f"*** Update File: {path}\n")
        for call_id, path in (
            ("call-1", "repeated.py"),
            ("call-2", "repeated.py"),
            ("call-3", "later.py"),
            ("call-4", "beyond-cap.py"),
        )
    ]
    _record_calls_and_touched_paths(accumulator, calls, cwd=None)

    activity = accumulator.as_activity()
    assert activity["tool_category_counts"] == {"edit": 4}
    assert activity["touched_files"] == ["repeated.py", "later.py"]


@pytest.mark.parametrize(
    ("same_call_paths", "expected_paths"),
    [
        (("a.py", "b.py"), ["a.py", "b.py", "c.py"]),
        (("b.py", "a.py"), ["b.py", "a.py", "c.py"]),
    ],
)
def test_touched_paths_merge_duplicate_calls_in_carrier_order(
    same_call_paths: tuple[str, str],
    expected_paths: list[str],
):
    accumulator = cu._CodexToolActivityAccumulator(touched_path_cap=3)
    calls = [
        ("duplicate-call", "apply_patch", f"*** Update File: {path}\n")
        for path in same_call_paths
    ]
    calls.append(("later-call", "apply_patch", "*** Update File: c.py\n"))
    _record_calls_and_touched_paths(accumulator, calls, cwd=None)

    activity = accumulator.as_activity()
    assert activity["tool_category_counts"] == {"edit": 2}
    assert activity["touched_files"] == expected_paths


@pytest.mark.parametrize(
    "call_order",
    [
        ("conflicting-patch", "conflicting-command", "valid-patch"),
        ("conflicting-command", "conflicting-patch", "valid-patch"),
        ("conflicting-patch", "valid-patch", "conflicting-command"),
        ("conflicting-command", "valid-patch", "conflicting-patch"),
    ],
)
def test_conflicting_call_identity_does_not_consume_touched_path_cap(
    call_order: tuple[str, str, str],
):
    accumulator = cu._CodexToolActivityAccumulator(touched_path_cap=1)
    carriers = {
        "conflicting-patch": (
            "ambiguous-call",
            "apply_patch",
            "*** Update File: blocked.py\n",
        ),
        "conflicting-command": (
            "ambiguous-call",
            "exec_command",
            json.dumps({"cmd": "pytest"}),
        ),
        "valid-patch": (
            "valid-call",
            "apply_patch",
            "*** Update File: kept.py\n",
        ),
    }
    _record_calls_and_touched_paths(
        accumulator,
        [carriers[carrier_name] for carrier_name in call_order],
        cwd=None,
    )

    activity = accumulator.as_activity()
    assert activity["tool_category_counts"] == {"edit": 1}
    assert activity["tool_names"] == [{"name": "apply_patch", "count": 1}]
    assert activity["touched_files"] == ["kept.py"]
    assert "commands" not in activity


def test_repeated_multi_file_calls_do_not_hide_later_distinct_path():
    accumulator = cu._CodexToolActivityAccumulator(
        carrier_cap=4,
        touched_path_cap=3,
    )
    repeated_patch = "*** Update File: a.py\n*** Update File: b.py\n"
    calls = [
        ("call-1", "apply_patch", repeated_patch),
        ("call-2", "apply_patch", repeated_patch),
        ("call-3", "apply_patch", "*** Update File: c.py\n"),
    ]
    _record_calls_and_touched_paths(accumulator, calls, cwd=None)

    assert accumulator.as_activity()["touched_files"] == ["a.py", "b.py", "c.py"]


def test_touched_path_projection_retains_only_the_output_cap():
    carrier_cap = 64
    touched_path_cap = 2
    accumulator = cu._CodexToolActivityAccumulator(
        carrier_cap=carrier_cap,
        touched_path_cap=touched_path_cap,
    )
    patch = "".join(
        f"*** Update File: path-{index}.py\n" for index in range(200)
    )
    calls = [
        (f"call-{call_index}", "apply_patch", patch)
        for call_index in range(carrier_cap)
    ]
    _record_calls_and_touched_paths(accumulator, calls, cwd=None)

    assert len(accumulator._carrier_identities_by_source_line) == carrier_cap
    assert len(accumulator._touched_path_set) == touched_path_cap
    assert all(
        not hasattr(fact, "touched_paths")
        for fact in accumulator._facts_by_call_id.values()
        if fact is not None
    )
    assert accumulator.as_activity()["touched_files"] == [
        "path-0.py",
        "path-1.py",
    ]


def test_touched_path_scan_discards_paths_if_source_changes():
    accumulator = cu._CodexToolActivityAccumulator()
    accumulator.record_call(
        "call-1",
        "apply_patch",
        "*** Update File: must-be-discarded.py\n",
        source_line_number=1,
    )
    accumulator.record_touched_paths(
        1,
        cu._CodexActionCarrier(
            call_id="call-1",
            action_name="apply_patch",
            raw_arguments="*** Update File: must-be-discarded.py\n",
        ),
        cwd=None,
    )
    accumulator.finish_touched_path_scan(source_unchanged=False)

    activity = accumulator.as_activity()
    assert activity["tool_category_counts"] == {"edit": 1}
    assert "touched_files" not in activity


def test_touched_path_scan_rejects_mutation_between_phases(tmp_path):
    def rollout_line(path: str) -> str:
        return json.dumps(
            {
                "type": "custom_tool_call",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "same-call",
                    "input": f"*** Update File: {path}\n",
                },
            }
        )

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(rollout_line("first.py") + "\n", encoding="utf-8")
    accumulator = cu._CodexToolActivityAccumulator()
    with rollout.open("r", encoding="utf-8") as handle:
        source_snapshot = cu._codex_open_file_snapshot(handle)
        first_record = json.loads(handle.readline())
        first_carrier = cu._codex_action_carrier(first_record["payload"])
        assert first_carrier is not None
        accumulator.record_call(
            first_carrier.call_id,
            first_carrier.action_name,
            first_carrier.raw_arguments,
            source_line_number=1,
        )

        # Same logical identity, different raw path. A mixed two-phase read must
        # omit paths rather than attributing either snapshot's argument.
        rollout.write_text(
            rollout_line("must-not-be-attributed.py") + "\n",
            encoding="utf-8",
        )
        cu._collect_codex_rollout_touched_paths(
            handle,
            source_snapshot=source_snapshot,
            source_line_count=1,
            tool_activity=accumulator,
            cwd=None,
        )

    activity = accumulator.as_activity()
    assert activity["tool_category_counts"] == {"edit": 1}
    assert "touched_files" not in activity


def test_anonymous_and_identified_calls_preserve_carrier_order():
    accumulator = cu._CodexToolActivityAccumulator()
    calls = [
        (None, "exec_command", json.dumps({"cmd": "first"})),
        ("identified", "exec_command", json.dumps({"cmd": "second"})),
        (None, "exec_command", json.dumps({"cmd": "third"})),
    ]
    _record_calls_and_touched_paths(accumulator, calls, cwd=None)

    assert accumulator.as_activity()["commands"] == ["first", "second", "third"]


def test_tool_activity_empty_calls_is_empty():
    assert _tool_activity_from_calls([], cwd=None) == {}


def test_tool_activity_carrier_cap_bounds_duplicate_decoding():
    accumulator = cu._CodexToolActivityAccumulator(carrier_cap=1)
    _record_calls_and_touched_paths(
        accumulator,
        [
            ("call-1", "exec_command", json.dumps({"cmd": "pytest -q"})),
            (
                "call-1",
                "apply_patch",
                "*** Add File: must-not-be-decoded.py\n",
            ),
        ],
        cwd=None,
    )

    assert accumulator.as_activity() == {
        "tool_category_counts": {"execute": 1},
        "tool_names": [{"name": "exec_command", "count": 1}],
        "commands": ["pytest -q"],
    }


# --- event build ------------------------------------------------------------


def test_build_discovery_event_shape_and_stable_id():
    activity = {
        "tool_category_counts": {"execute": 2},
        "tool_names": [{"name": "exec", "count": 2}],
        "commands": ["ls", "pwd"],
        "touched_files": ["a.py"],
    }
    event = build_discovery_tool_activity_event(
        client="codex", session_id="sess-1", activity=activity, captured_at=123.0
    )
    assert event["event_type"] == TOOL_ACTIVITY_EVENT_TYPE
    assert event["source"] == "codex"
    assert is_discovery_tool_activity_event(event)
    md = event["metadata"]
    assert md["client"] == "codex"
    assert md["client_session_id"] == "sess-1"
    assert md["capture_basis"] == DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS
    assert md["commands"] == ["ls", "pwd"]
    assert md["touched_files"] == ["a.py"]
    # Stable per (client, session): a re-scan yields the SAME id so a scoped
    # replace refreshes rather than duplicates.
    again = build_discovery_tool_activity_event(
        client="codex", session_id="sess-1", activity=activity, captured_at=999.0
    )
    assert again["event_id"] == event["event_id"]


def test_build_discovery_event_none_when_no_signal():
    assert build_discovery_tool_activity_event(
        client="codex", session_id="s", activity=None, captured_at=1.0
    ) is None
    assert build_discovery_tool_activity_event(
        client="codex", session_id="s", activity={}, captured_at=1.0
    ) is None


def test_transcript_scan_supersedes_hook_events_no_double_count():
    # A codex session's PreToolUse hook may fire for SOME tools, producing a
    # hook tool_activity event; the discovery scan then emits a FULL-transcript
    # event for the same session. The additive counters must NOT sum both (the
    # scan is a superset) — the scan supersedes the hook for that session.
    hook_event = {
        "event_id": "toolact:hook",
        "event_type": TOOL_ACTIVITY_EVENT_TYPE,
        "source": "codex",
        "metadata": {
            "client": "codex",
            "client_session_id": "sess-dup",
            "capture_basis": "client_hook_tool_category",
            "tool_category_counts": {"execute": 2},
            "tool_names": [{"name": "exec", "count": 2}],
        },
    }
    scan_event = build_discovery_tool_activity_event(
        client="codex",
        session_id="sess-dup",
        activity={
            "tool_category_counts": {"execute": 5, "edit": 3},
            "tool_names": [{"name": "exec", "count": 5}, {"name": "apply_patch", "count": 3}],
        },
        captured_at=1.0,
    )
    # Try BOTH event orders — the supersede must hold regardless of log order.
    for events in ([hook_event, scan_event], [scan_event, hook_event]):
        categories = build_tool_activity_by_session(events)[("codex", "sess-dup")]
        names = build_tool_names_by_session(events)[("codex", "sess-dup")]
        assert categories == {"execute": 5, "edit": 3}  # not execute=7
        assert names == {"exec": 5, "apply_patch": 3}  # not exec=7

    # A DIFFERENT codex session with only a hook event is unaffected.
    other_hook = {
        "event_id": "toolact:hook2",
        "event_type": TOOL_ACTIVITY_EVENT_TYPE,
        "source": "codex",
        "metadata": {
            "client": "codex",
            "client_session_id": "sess-hook-only",
            "capture_basis": "client_hook_tool_category",
            "tool_category_counts": {"execute": 4},
        },
    }
    both = build_tool_activity_by_session([hook_event, scan_event, other_hook])
    assert both[("codex", "sess-hook-only")] == {"execute": 4}


def test_discovery_event_flows_through_actions_builders():
    # The whole point: a discovery event feeds the SAME client-agnostic Actions
    # builders the hook path uses, with zero changes to them.
    event = build_discovery_tool_activity_event(
        client="codex",
        session_id="sess-9",
        activity={
            "tool_category_counts": {"execute": 3, "edit": 1},
            "commands": ["git status"],
            "touched_files": ["src/a.py"],
        },
        captured_at=1.0,
    )
    events = [event]
    assert build_commands_by_session(events)[("codex", "sess-9")] == ["git status"]
    assert build_touched_files_by_session(events)[("codex", "sess-9")] == ["src/a.py"]
    assert build_tool_activity_by_session(events)[("codex", "sess-9")] == {
        "execute": 3,
        "edit": 1,
    }


# --- integration: discovery populates the observation carrier ---------------


def _rewrite_rollout_with_tool_calls(codex_home, session_id, cwd, lines):
    rollout = next((codex_home / "sessions").rglob(f"*{session_id}.jsonl"))
    body = [
        json.dumps(
            {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}}
        ),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 0,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 120,
                        }
                    },
                    "model": "gpt-5.5",
                },
            }
        ),
        *[json.dumps(line) for line in lines],
    ]
    rollout.write_text("\n".join(body) + "\n", encoding="utf-8")


def test_discover_codex_populates_rollout_tool_activity(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home, session_id="worker", updated_at=300, model="gpt-5.5"
    )
    _rewrite_rollout_with_tool_calls(
        codex_home,
        "worker",
        "/work/project",
        [
            {
                "type": "custom_tool_call",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "c1",
                    "input": 'tools.exec_command({cmd:"pytest -q", workdir:"/work/project"});',
                },
            },
            {
                "type": "custom_tool_call",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "c2",
                    "input": "*** Begin Patch\n*** Update File: src/worker.py\n+x\n*** End Patch\n",
                },
            },
        ],
    )

    observations: list = []
    discover_codex_usage(
        codex_home=codex_home, limit_sessions=10, _session_observations=observations
    )
    by_id = {o.client_session_id: o for o in observations}
    worker = by_id["worker"]
    activity = worker.rollout_tool_activity
    assert activity["commands"] == ["pytest -q"]
    assert activity["touched_files"] == ["src/worker.py"]
    assert activity["tool_category_counts"] == {"edit": 1, "execute": 1}

    # The carrier is INTERNAL: it never leaks into the session observation event.
    assert "rollout_tool_activity" not in worker.to_sentinel_event()["metadata"]
    assert "tool_activity" not in worker.to_sentinel_event()["metadata"]


def test_touched_path_before_session_meta_uses_authoritative_session_cwd(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home, session_id="worker", updated_at=300, model="gpt-5.5"
    )
    rollout = next((codex_home / "sessions").rglob("*worker.jsonl"))
    rollout.write_text(
        "\n".join(
            json.dumps(line)
            for line in [
                {
                    "type": "custom_tool_call",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "call_id": "pre-meta-call",
                        "input": (
                            "*** Begin Patch\n"
                            "*** Update File: /work/project/src/pre_meta.py\n"
                            "*** End Patch\n"
                        ),
                    },
                },
                {
                    "type": "session_meta",
                    "payload": {"id": "worker", "cwd": "/work/project"},
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 0,
                                "output_tokens": 20,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 120,
                            }
                        },
                        "model": "gpt-5.5",
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    observations: list = []
    discover_codex_usage(
        codex_home=codex_home,
        limit_sessions=10,
        _session_observations=observations,
    )

    worker = next(o for o in observations if o.client_session_id == "worker")
    assert worker.rollout_tool_activity["touched_files"] == ["src/pre_meta.py"]


def test_discover_codex_populates_paginated_mcp_tool_activity_once(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home, session_id="worker", updated_at=300, model="gpt-5.5"
    )
    paginated_call = {
        "timestamp": "2026-07-05T10:00:01.000Z",
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "thread_id": "thread-redacted",
            "turn_id": "turn-redacted",
            "item": {
                "type": "McpToolCall",
                "id": "call_section",
                "server": "agentacct",
                "tool": "agentacct_record_section",
                "arguments": {"section_id": "redacted"},
                "status": "completed",
                "duration": {"secs": 2, "nanos": 0},
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"event": {"event_id": "evt_123456789abc"}}
                            ),
                        }
                    ]
                },
            },
        },
    }
    # The duplicate legacy terminal fragment carries the same logical call ID.
    # Actions count logical calls, not rollout representations.
    legacy_duplicate = {
        "type": "event_msg",
        "payload": {
            "type": "mcp_tool_call_end",
            "call_id": "call_section",
            "invocation": {
                "server": "agentacct",
                "tool": "agentacct_record_section",
                "arguments": {"section_id": "redacted"},
            },
            "result": {
                "Ok": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"event": {"event_id": "evt_123456789abc"}}
                            ),
                        }
                    ]
                }
            },
        },
    }
    _rewrite_rollout_with_tool_calls(
        codex_home,
        "worker",
        "/work/project",
        [paginated_call, legacy_duplicate],
    )

    observations: list = []
    discover_codex_usage(
        codex_home=codex_home, limit_sessions=10, _session_observations=observations
    )

    worker = next(o for o in observations if o.client_session_id == "worker")
    assert worker.rollout_tool_activity["tool_category_counts"] == {"mcp": 1}
    assert worker.rollout_tool_activity["tool_names"] == [
        {"name": "mcp__agentacct__agentacct_record_section", "count": 1}
    ]


def test_import_local_emits_and_refreshes_codex_actions(tmp_path):
    from typer.testing import CliRunner

    from agentacct.cli import app
    from agentacct.service import SentinelService

    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home, session_id="worker", updated_at=300, model="gpt-5.5"
    )
    _rewrite_rollout_with_tool_calls(
        codex_home,
        "worker",
        "/work/project",
        [
            {
                "type": "custom_tool_call",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "c1",
                    "input": 'tools.exec_command({cmd:"pytest -q"});',
                },
            },
            {
                "type": "custom_tool_call",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "c2",
                    "input": "*** Begin Patch\n*** Update File: src/worker.py\n+x\n*** End Patch\n",
                },
            },
        ],
    )
    store = tmp_path / "state"
    args = [
        "usage",
        "import-local",
        "--client",
        "codex",
        "--store-dir",
        str(store),
        "--codex-home",
        str(codex_home),
        "--json",
    ]

    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output

    def _codex_activity_events(store_dir):
        return [
            event
            for event in SentinelService(store_dir).list_all_events()
            if is_discovery_tool_activity_event(event)
            and event["metadata"].get("client") == "codex"
            and event["metadata"].get("client_session_id") == "worker"
        ]

    events = _codex_activity_events(store)
    assert len(events) == 1
    metadata = events[0]["metadata"]
    assert metadata["commands"] == ["pytest -q"]
    assert metadata["touched_files"] == ["src/worker.py"]
    assert metadata["tool_category_counts"] == {"edit": 1, "execute": 1}

    # The session grows (a second command runs), then re-import. The scoped
    # replace REFRESHES the session's Actions in place: still exactly ONE event
    # (never doubled/accumulated), and now reflecting the NEW command — a
    # still-growing session is never frozen at its first-seen state.
    _rewrite_rollout_with_tool_calls(
        codex_home,
        "worker",
        "/work/project",
        [
            {
                "type": "custom_tool_call",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "c1",
                    "input": 'tools.exec_command({cmd:"pytest -q"});',
                },
            },
            {
                "type": "custom_tool_call",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "c3",
                    "input": 'tools.exec_command({cmd:"ruff check"});',
                },
            },
        ],
    )
    second = CliRunner().invoke(app, args)
    assert second.exit_code == 0, second.output
    refreshed = _codex_activity_events(store)
    assert len(refreshed) == 1
    assert refreshed[0]["metadata"]["commands"] == ["pytest -q", "ruff check"]
