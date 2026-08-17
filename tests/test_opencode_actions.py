"""Discovery-side OpenCode Actions + verification extraction.

OpenCode's observe-only plugin barely fires for its built-in tools, so these
signals are derived from the ``part`` table (its on-disk tool-call log) at
discovery time instead — the same store the token importer already reads. Unlike
Codex, OpenCode records a bash tool's harness ``exit`` code, so its checks lift a
step to ``independently_checked`` (not just self_checked).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agentacct import client_usage as cu
from agentacct.client_usage import discover_opencode_usage
from agentacct.mechanical_capture import build_discovery_mechanical_check_envelopes
from agentacct.mechanical_checks import build_mechanical_check_events
from agentacct.task_outcome import step_evidence_grade
from agentacct.tool_activity import (
    build_commands_by_session,
    build_tool_activity_by_session,
    build_tool_names_by_session,
    build_touched_files_by_session,
    is_discovery_tool_activity_event,
)

from tests.test_client_usage import _make_opencode_db_home


# --- part-row fixtures ------------------------------------------------------


def _tool_part(
    session_id: str,
    tool: str,
    *,
    status: str = "completed",
    command: str | None = None,
    exit_code: object = None,
    file_path: str | None = None,
    patch_text: str | None = None,
    files: list[dict[str, object]] | None = None,
    time_end: int | None = None,
) -> dict[str, object]:
    """One OpenCode ``part`` row in its real ``{"type":"tool","state":{...}}`` shape."""

    state_input: dict[str, object] = {}
    if command is not None:
        state_input["command"] = command
    if file_path is not None:
        state_input["filePath"] = file_path
    if patch_text is not None:
        state_input["patchText"] = patch_text
    metadata: dict[str, object] = {}
    if exit_code is not None:
        metadata["exit"] = exit_code
    if files is not None:
        metadata["files"] = files
    state: dict[str, object] = {"status": status, "input": state_input}
    if metadata:
        state["metadata"] = metadata
    if time_end is not None:
        state["time"] = {"start": time_end - 1, "end": time_end}
    return {
        "session_id": session_id,
        "data": json.dumps({"type": "tool", "tool": tool, "state": state}),
    }


def _add_opencode_tool_parts(
    opencode_home: Path,
    parts: list[dict[str, object]],
    *,
    db_name: str = "opencode.db",
) -> None:
    """Create the ``part`` table and insert pre-assembled tool rows (from ``_tool_part``)."""

    con = sqlite3.connect(opencode_home / db_name)
    try:
        con.execute(
            "create table part (id text primary key, message_id text, session_id text, "
            "time_created integer, time_updated integer, data text)"
        )
        for index, part in enumerate(parts):
            con.execute(
                "insert into part (id, message_id, session_id, time_created, time_updated, data) "
                "values (?, ?, ?, ?, ?, ?)",
                (
                    f"prt_{index}",
                    f"msg_{index}",
                    part.get("session_id"),
                    index,
                    index,
                    part.get("data"),
                ),
            )
        con.commit()
    finally:
        con.close()


def _session_row(session_id: str, directory: str) -> dict[str, object]:
    return {
        "id": session_id,
        "project_id": "global",
        "parent_id": None,
        "directory": directory,
        "title": "s",
        "cost": 0.0,
        "tokens_input": 100,
        "tokens_output": 20,
        "tokens_reasoning": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "model": json.dumps({"id": "gpt-5", "providerID": "openai"}),
        "time_created": 1_785_248_000_000,
        "time_updated": 1_785_249_000_000,
    }


# --- pure extraction: edit paths --------------------------------------------


def test_edit_paths_from_filepath_files_and_patch_body():
    part = {
        "file_path": "/w/proj/src/edit.py",
        "files_json": json.dumps(
            [{"filePath": "/w/proj/src/patched.py", "type": "update"}]
        ),
        "patch_text": "*** Begin Patch\n*** Add File: notes.txt\n+hi\n*** End Patch",
    }
    assert cu._opencode_edit_paths(part) == [
        "/w/proj/src/edit.py",
        "/w/proj/src/patched.py",
        "notes.txt",
    ]


def test_edit_paths_ignore_missing_and_malformed():
    assert cu._opencode_edit_paths({}) == []
    assert cu._opencode_edit_paths(
        {"file_path": "  ", "files_json": "not json", "patch_text": None}
    ) == []
    # A files list whose entries are not path-bearing yields nothing.
    assert cu._opencode_edit_paths({"files_json": json.dumps([{"type": "x"}, 5])}) == []


# --- pure extraction: check tick --------------------------------------------


def _bash(**over) -> dict[str, object]:
    base: dict[str, object] = {
        "tool": "bash",
        "status": "completed",
        "command": "npm test",
        "exit_code": 0,
        "time_end": 1_785_248_957_928,
        "row_time": 1_785_248_957_928,
    }
    base.update(over)
    return base


def test_check_tick_recognizes_check_and_converts_ms_to_seconds():
    tick = cu._opencode_check_tick(_bash(), session_id="ses_1")
    assert tick is not None
    assert tick["c"] == "opencode" and tick["s"] == "ses_1"
    assert tick["k"] == "test" and tick["r"] == "npm" and tick["x"] == 0
    # OpenCode stamps MILLISECONDS; the tick is epoch SECONDS (matching time.time()).
    assert tick["t"] == 1_785_248_957_928 / 1000.0
    assert tick["d"].startswith("sha256:")


def test_check_tick_failing_exit_is_kept():
    tick = cu._opencode_check_tick(_bash(command="npm run build", exit_code=2), session_id="s")
    assert tick["k"] == "build" and tick["x"] == 2


def test_check_tick_rejects_non_check_and_masked_and_bad_exit():
    assert cu._opencode_check_tick(_bash(command="git status"), session_id="s") is None
    # A masked runner (``|| true``) can exit 0 while the check failed → not captured.
    assert cu._opencode_check_tick(_bash(command="npm test || true"), session_id="s") is None
    assert cu._opencode_check_tick(_bash(status="error"), session_id="s") is None
    assert cu._opencode_check_tick(_bash(exit_code=True), session_id="s") is None  # bool
    assert cu._opencode_check_tick(_bash(exit_code="0"), session_id="s") is None  # str
    assert cu._opencode_check_tick(_bash(command=None), session_id="s") is None


def test_check_tick_falls_back_to_row_time_then_drops_when_no_timestamp():
    tick = cu._opencode_check_tick(
        _bash(time_end=None, row_time=1_785_000_000_000), session_id="s"
    )
    assert tick["t"] == 1_785_000_000_000 / 1000.0
    assert cu._opencode_check_tick(_bash(time_end=None, row_time=None), session_id="s") is None
    assert cu._opencode_check_tick(_bash(time_end=0, row_time=0), session_id="s") is None


# --- pure extraction: per-session aggregation -------------------------------


def test_actions_from_parts_aggregates_all_signals_and_excludes_read_from_touched():
    parts = [
        {"tool": "bash", "status": "completed", "command": "npm test", "exit_code": 0,
         "time_end": 1_785_248_957_928, "row_time": 1_785_248_957_928},
        {"tool": "bash", "status": "completed", "command": "git status", "exit_code": 0,
         "time_end": 1_785_248_958_000, "row_time": 1_785_248_958_000},
        {"tool": "read", "status": "completed", "file_path": "/w/proj/README.md"},
        {"tool": "apply_patch", "status": "completed",
         "patch_text": "*** Add File: notes.txt\n",
         "files_json": json.dumps([{"filePath": "/w/proj/src/a.py", "type": "update"}])},
        {"tool": "glob", "status": "completed"},
    ]
    activity, checks = cu._opencode_actions_from_parts(
        parts, cwd="/w/proj", session_id="ses_1"
    )
    assert activity["tool_category_counts"] == {
        "edit": 1,
        "execute": 2,
        "read": 1,
        "search": 1,
    }
    names = {entry["name"]: entry["count"] for entry in activity["tool_names"]}
    assert names == {"bash": 2, "read": 1, "apply_patch": 1, "glob": 1}
    # Both bash commands captured; only the check runner (npm test) is a check.
    assert activity["commands"] == ["npm test", "git status"]
    # read's filePath is NEVER a touched file; the edit paths are relativized
    # (structured metadata files before the raw patch body, insertion-ordered).
    assert activity["touched_files"] == ["src/a.py", "notes.txt"]
    assert [(c["k"], c["r"], c["x"]) for c in checks] == [("test", "npm", 0)]


def test_actions_from_parts_dedupes_commands_and_paths():
    parts = [
        {"tool": "bash", "status": "completed", "command": "ls", "exit_code": 0,
         "time_end": 1, "row_time": 1},
        {"tool": "bash", "status": "completed", "command": "ls", "exit_code": 0,
         "time_end": 2, "row_time": 2},
        {"tool": "edit", "status": "completed", "file_path": "a.py"},
        {"tool": "edit", "status": "completed", "file_path": "a.py"},
    ]
    activity, _ = cu._opencode_actions_from_parts(parts, cwd=None, session_id="s")
    assert activity["commands"] == ["ls"]
    assert activity["touched_files"] == ["a.py"]


def test_actions_from_parts_empty_is_empty():
    activity, checks = cu._opencode_actions_from_parts([], cwd=None, session_id="s")
    assert activity == {}
    assert checks == []


# --- integration: discovery populates the carriers --------------------------


def test_discover_opencode_populates_rollout_carriers(tmp_path):
    home = _make_opencode_db_home(
        tmp_path, rows=[_session_row("ses_a", "/w/proj")]
    )
    _add_opencode_tool_parts(
        home,
        [
            _tool_part("ses_a", "bash", command="npm test", exit_code=0,
                       time_end=1_785_248_957_928),
            _tool_part("ses_a", "apply_patch",
                       patch_text="*** Begin Patch\n*** Update File: src/w.py\n+x\n*** End Patch\n",
                       files=[{"filePath": "/w/proj/src/w.py", "type": "update"}]),
            _tool_part("ses_a", "read", file_path="/w/proj/README.md"),
        ],
    )
    events = discover_opencode_usage(opencode_home=home, limit_sessions=10)
    event = next(e for e in events if e.client_session_id == "ses_a")
    activity = event.rollout_tool_activity
    assert activity["commands"] == ["npm test"]
    assert activity["touched_files"] == ["src/w.py"]
    assert activity["tool_category_counts"] == {"edit": 1, "execute": 1, "read": 1}
    checks = event.rollout_mechanical_checks
    assert [(c["k"], c["r"], c["x"]) for c in checks] == [("test", "npm", 0)]

    # The carriers are INTERNAL: they never leak into the usage sentinel event.
    metadata = event.to_sentinel_event()["metadata"]
    assert "rollout_tool_activity" not in metadata
    assert "rollout_mechanical_checks" not in metadata


def test_discover_tolerates_malformed_and_null_part_data(tmp_path):
    # The OpenCode db is read while OpenCode may be mid-write, so a part.data cell
    # can be a truncated/partial write. A malformed or NULL row must be SKIPPED,
    # never abort the scan (a bare json_extract in SQL raises OperationalError,
    # which would zero the client's already-read usage). One clean bash row still
    # yields its command + check.
    home = _make_opencode_db_home(tmp_path, rows=[_session_row("ses_a", "/w/proj")])
    con = sqlite3.connect(home / "opencode.db")
    con.execute(
        "create table part (id text primary key, message_id text, session_id text, "
        "time_created integer, time_updated integer, data text)"
    )
    good = _tool_part("ses_a", "bash", command="npm test", exit_code=0, time_end=5)["data"]
    con.executemany(
        "insert into part (id, message_id, session_id, time_created, time_updated, data) "
        "values (?, ?, ?, ?, ?, ?)",
        [
            ("p0", "m0", "ses_a", 1, 1, "plain text, a torn write"),  # malformed JSON
            ("p1", "m1", "ses_a", 2, 2, None),  # NULL data
            ("p2", "m2", "ses_a", 3, 5, good),  # a clean tool row
            ("p3", "m3", "ses_a", 4, 4, '{"type":"reasoning"}'),  # valid non-tool
        ],
    )
    con.commit()
    con.close()
    events = discover_opencode_usage(opencode_home=home, limit_sessions=10)
    event = next(e for e in events if e.client_session_id == "ses_a")
    assert event.rollout_tool_activity["commands"] == ["npm test"]
    assert [(c["k"], c["r"]) for c in event.rollout_mechanical_checks] == [("test", "npm")]


def _import_args(store: Path, home: Path) -> list[str]:
    return [
        "usage", "import-local", "--client", "opencode",
        "--store-dir", str(store), "--opencode-home", str(home), "--json",
    ]


def test_import_local_emits_and_refreshes_opencode_actions(tmp_path):
    from typer.testing import CliRunner

    from agentacct.cli import app
    from agentacct.service import SentinelService

    home = _make_opencode_db_home(tmp_path, rows=[_session_row("ses_a", "/w/proj")])
    _add_opencode_tool_parts(
        home,
        [
            _tool_part("ses_a", "bash", command="npm test", exit_code=0,
                       time_end=1_785_248_957_928),
            _tool_part("ses_a", "apply_patch",
                       files=[{"filePath": "/w/proj/src/w.py", "type": "update"}]),
        ],
    )
    store = tmp_path / "state"
    first = CliRunner().invoke(app, _import_args(store, home))
    assert first.exit_code == 0, first.output

    def _activity_events():
        return [
            event
            for event in SentinelService(store).list_all_events()
            if is_discovery_tool_activity_event(event)
            and event["metadata"].get("client") == "opencode"
            and event["metadata"].get("client_session_id") == "ses_a"
        ]

    events = _activity_events()
    assert len(events) == 1
    md = events[0]["metadata"]
    assert md["commands"] == ["npm test"]
    assert md["touched_files"] == ["src/w.py"]
    assert md["tool_category_counts"] == {"edit": 1, "execute": 1}

    # The session grows; a re-import REFRESHES its Actions in place (one event,
    # never doubled), now reflecting the new command.
    con = sqlite3.connect(home / "opencode.db")
    con.execute(
        "insert into part (id, message_id, session_id, time_created, time_updated, data) "
        "values (?, ?, ?, ?, ?, ?)",
        ("prt_new", "msg_new", "ses_a", 9, 9,
         _tool_part("ses_a", "bash", command="ruff check", exit_code=0, time_end=2)["data"]),
    )
    con.commit()
    con.close()
    second = CliRunner().invoke(app, _import_args(store, home))
    assert second.exit_code == 0, second.output
    refreshed = _activity_events()
    assert len(refreshed) == 1
    assert refreshed[0]["metadata"]["commands"] == ["npm test", "ruff check"]


def test_import_local_emits_opencode_mechanical_check_idempotently(tmp_path):
    from typer.testing import CliRunner

    from agentacct.cli import app
    from agentacct.service import SentinelService

    home = _make_opencode_db_home(tmp_path, rows=[_session_row("ses_a", "/w/proj")])
    _add_opencode_tool_parts(
        home,
        [
            _tool_part("ses_a", "bash", command="npm test", exit_code=0,
                       time_end=1_785_248_957_928),
            _tool_part("ses_a", "bash", command="echo hi", exit_code=0, time_end=2),
        ],
    )
    store = tmp_path / "state"
    assert CliRunner().invoke(app, _import_args(store, home)).exit_code == 0

    def _checks():
        return SentinelService(store).evidence.envelopes(
            event_type="machine_check_observed"
        )

    checks = _checks()
    assert len(checks) == 1  # only the recognized check runner (echo hi is not one)
    env = checks[0]
    assert env.source_type == "client_hook"
    assert env.source_system == "opencode"
    assert env.source_schema == "opencode_tool_execute_after.v1"
    assert env.subjects.to_dict()["client_session_id"] == "ses_a"
    attrs = env.payload["attributes"]
    assert attrs["check_kind"] == "test" and attrs["runner"] == "npm"
    assert attrs["exit_code"] == 0 and attrs["passed"] is True

    # A re-import re-derives a byte-identical check (stable DB timestamp) → the
    # content idempotency key dedupes; still exactly one, never double-recorded.
    assert CliRunner().invoke(app, _import_args(store, home)).exit_code == 0
    assert len(_checks()) == 1


def test_produced_opencode_check_lifts_step_to_independently_checked():
    # The produced envelope is the right shape to flow through the existing
    # mechanical-check projection and lift a completed step past self_checked.
    ticks = [
        {"c": "opencode", "s": "ses_a", "k": "test", "r": "npm",
         "d": "sha256:" + "a" * 64, "x": 0, "t": 1_785_248_957.928}
    ]
    envelopes = build_discovery_mechanical_check_envelopes(ticks)
    checks = build_mechanical_check_events(envelopes)
    assert len(checks) == 1
    assert checks[0]["source_type"] == "client_hook"
    grade = step_evidence_grade(
        {"latest_status": "completed", "current_check_events": checks}
    )["grade"]
    assert grade == "independently_checked"


def test_produced_check_attaches_to_check_relevant_step_via_session_time():
    # End to end through the REAL session-time attribution path: a produced
    # opencode check attaches to a check-relevant step begun in its session at or
    # before its time, and is honestly declined for a research-kind step.
    from agentacct.api import _link_mechanical_checks_by_session_time

    check = build_mechanical_check_events(
        build_discovery_mechanical_check_envelopes(
            [{"c": "opencode", "s": "ses_a", "k": "test", "r": "npm",
              "d": "sha256:" + "a" * 64, "x": 0, "t": 1_785_248_957.928}]
        )
    )[0]

    def _projection(kind: str) -> dict:
        return {
            "tasks": [
                {
                    "task_id": "t1",
                    "work_items": [
                        {
                            "client_session_id": "ses_a",
                            "kind": kind,
                            "latest_status": "completed",
                            "started_at": 1_785_248_957.0,  # begun before the check
                        }
                    ],
                    "current_check_events": [check],
                }
            ]
        }

    # A check-relevant (implementation) step gets the check → independently_checked.
    impl = _link_mechanical_checks_by_session_time(_projection("implementation"))
    step = impl["tasks"][0]["work_items"][0]
    assert len(step.get("current_check_events") or []) == 1
    assert step_evidence_grade(step)["grade"] == "independently_checked"

    # A research step never gets a test/build check placed on it (honesty guard),
    # matching the live receipt where a repo-overview step left the check
    # unattributed rather than crediting research work.
    research = _link_mechanical_checks_by_session_time(_projection("research"))
    assert not (research["tasks"][0]["work_items"][0].get("current_check_events") or [])


# --- privacy + supersession -------------------------------------------------


def test_discovery_activity_supersedes_opencode_hook_events_no_double_count():
    # If an OpenCode hook event and the discovery scan both cover a session, the
    # additive counters must not sum both (the scan is a superset). The existing
    # client-agnostic supersession handles OpenCode with no per-client code.
    hook_event = {
        "event_id": "toolact:hook",
        "event_type": "tool_activity_observed",
        "source": "opencode",
        "metadata": {
            "client": "opencode",
            "client_session_id": "ses_dup",
            "capture_basis": "client_hook_tool_category",
            "tool_category_counts": {"execute": 2},
        },
    }
    from agentacct.tool_activity import build_discovery_tool_activity_event

    scan_event = build_discovery_tool_activity_event(
        client="opencode",
        session_id="ses_dup",
        activity={"tool_category_counts": {"execute": 5, "edit": 1}},
        captured_at=1.0,
    )
    for events in ([hook_event, scan_event], [scan_event, hook_event]):
        assert build_tool_activity_by_session(events)[("opencode", "ses_dup")] == {
            "execute": 5,
            "edit": 1,
        }


def test_discovery_activity_flows_through_actions_builders():
    from agentacct.tool_activity import build_discovery_tool_activity_event

    event = build_discovery_tool_activity_event(
        client="opencode",
        session_id="ses_b",
        activity={
            "tool_category_counts": {"execute": 3, "edit": 1},
            "commands": ["npm test"],
            "touched_files": ["src/a.py"],
        },
        captured_at=1.0,
    )
    events = [event]
    assert build_commands_by_session(events)[("opencode", "ses_b")] == ["npm test"]
    assert build_touched_files_by_session(events)[("opencode", "ses_b")] == ["src/a.py"]
    assert build_tool_names_by_session(events) == {}
