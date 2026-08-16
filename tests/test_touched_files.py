"""Hook-captured touched files (Actions dimension).

A file-EDIT tool's destination path is captured by the tool-activity hook,
relativized to the session cwd (never an absolute prefix, home dir, or username,
never the content/diff/args), spooled, drained onto the tool_activity_observed
event, aggregated per session, and unioned into the Task's Actions touched files
alongside the agent-reported MCP section/check files.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from agentacct.hooks import _edit_target_relpath, capture_tool_activity
from agentacct.task_projection import build_task_projection
from agentacct.tool_activity import (
    _normalize_touched_path,
    build_touched_files_by_session,
    drain_tool_activity_spool,
    record_tool_activity_tick,
)


def _edit_event(file_path: str, *, cwd: str = "/Users/u/repo", tool: str = "Edit") -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "session_id": "s1",
        "cwd": cwd,
        "tool_input": {"file_path": file_path},
    }


# ---------------------------------------------------------------------------
# _edit_target_relpath — the capture-time extraction + relativization
# ---------------------------------------------------------------------------


def test_relpath_relativizes_absolute_path_under_cwd() -> None:
    assert _edit_target_relpath(_edit_event("/Users/u/repo/src/foo.py"), "edit") == "src/foo.py"


def test_relpath_keeps_already_relative_path() -> None:
    assert _edit_target_relpath(_edit_event("src/bar.py"), "edit") == "src/bar.py"


def test_relpath_drops_path_outside_cwd() -> None:
    # A path outside the working tree relativizes to ../ and is dropped — never leaks
    # /etc/passwd or a sibling repo.
    assert _edit_target_relpath(_edit_event("/etc/passwd"), "edit") is None


def test_relpath_drops_absolute_without_cwd() -> None:
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "/x/y.py"}}
    assert _edit_target_relpath(ev, "edit") is None


def test_relpath_only_for_edit_category() -> None:
    assert _edit_target_relpath(_edit_event("/Users/u/repo/a.py", tool="Read"), "read") is None


def test_relpath_none_when_no_path_arg() -> None:
    ev = {"tool_name": "Bash", "cwd": "/Users/u/repo", "tool_input": {"command": "ls"}}
    assert _edit_target_relpath(ev, "execute") is None


def test_relpath_reads_notebook_path_key() -> None:
    ev = {"tool_name": "NotebookEdit", "cwd": "/Users/u/repo", "tool_input": {"notebook_path": "/Users/u/repo/nb.ipynb"}}
    assert _edit_target_relpath(ev, "edit") == "nb.ipynb"


# ---------------------------------------------------------------------------
# _normalize_touched_path — the tick-time defensive gate
# ---------------------------------------------------------------------------


def test_normalize_rejects_absolute_and_escape_and_drive() -> None:
    assert _normalize_touched_path("/etc/passwd") is None
    assert _normalize_touched_path("../../secret") is None
    assert _normalize_touched_path("C:\\Users\\x") is None
    assert _normalize_touched_path("\\\\server\\share") is None  # UNC (double backslash)
    assert _normalize_touched_path("\\Users\\bob\\secret\\id_rsa") is None  # single-backslash Windows absolute
    assert _normalize_touched_path("a\x00b") is None


def test_capture_drops_windows_drive_relative_absolute_end_to_end() -> None:
    # A single-leading-backslash Windows drive-relative path must never be stored —
    # it would otherwise leak the username + an out-of-tree location.
    store = _store()
    event = {"tool_name": "Edit", "session_id": "s1", "cwd": "C:\\Users\\bob\\proj", "tool_input": {"file_path": "\\Users\\bob\\secret\\id_rsa"}}
    capture_tool_activity(json.dumps(event), store_dir=store, client="claude-code")
    events = drain_tool_activity_spool(store)
    assert events == [] or "touched_files" not in events[0]["metadata"]


def test_normalize_keeps_and_cleans_relative() -> None:
    assert _normalize_touched_path("./src//foo.py") == "src/foo.py"
    assert _normalize_touched_path("src\\win\\bar.py") == "src/win/bar.py"


def test_normalize_length_bounded() -> None:
    long = "a/" * 300
    out = _normalize_touched_path(long)
    assert out is not None and len(out) <= 240


# ---------------------------------------------------------------------------
# pipeline: capture -> tick -> drain -> event.touched_files
# ---------------------------------------------------------------------------


def _store() -> Path:
    return Path(tempfile.mkdtemp())


def test_capture_records_edit_paths_deduped_edit_tools_only() -> None:
    store = _store()
    capture_tool_activity(json.dumps(_edit_event("/Users/u/repo/src/auth/login.py")), store_dir=store, client="claude-code")
    capture_tool_activity(json.dumps(_edit_event("/Users/u/repo/src/auth/login.py")), store_dir=store, client="claude-code")  # dup
    capture_tool_activity(json.dumps(_edit_event("/Users/u/repo/README.md", tool="Write")), store_dir=store, client="claude-code")
    capture_tool_activity(json.dumps({"tool_name": "Bash", "session_id": "s1", "cwd": "/Users/u/repo", "tool_input": {"command": "pytest"}}), store_dir=store, client="claude-code")
    events = drain_tool_activity_spool(store)
    assert len(events) == 1
    md = events[0]["metadata"]
    assert md["touched_files"] == ["src/auth/login.py", "README.md"]  # deduped, edit tools only, cwd-relative


def test_pipeline_never_stores_absolute_even_from_a_buggy_tick() -> None:
    # A tick handed an absolute path directly (bypassing the hook helper) is still
    # dropped by the tick's own _normalize_touched_path — no absolute path can persist.
    store = _store()
    record_tool_activity_tick(store, client="claude-code", session_id="s1", category="edit", name="Edit", path="/Users/secret/home/x.py", at=1.0)
    events = drain_tool_activity_spool(store)
    assert events == [] or "touched_files" not in events[0]["metadata"]


def test_build_touched_files_by_session_unions_across_batches() -> None:
    def _event(paths: list[str]) -> dict[str, Any]:
        return {
            "event_type": "tool_activity_observed",
            "metadata": {"client": "hermes", "client_session_id": "20260813_x", "touched_files": paths},
        }

    result = build_touched_files_by_session([_event(["a.py", "b.py"]), _event(["b.py", "c.py"])])
    assert result[("hermes", "20260813_x")] == ["a.py", "b.py", "c.py"]  # additive + deduped


# ---------------------------------------------------------------------------
# projection: session touched_files union into the Task Actions dimension
# ---------------------------------------------------------------------------


def _session_with_touched(files: list[str]) -> dict[str, Any]:
    return {
        "client": "hermes",
        "client_session_id": "sess-1",
        "identity_scope_state": "unscoped",
        "namespace_fingerprint": None,
        "session_kind": "root",
        "last_activity_at": 1.0,
        "related": {"parent": None},
        "usage": {"rows": 1, "priced_rows": 1, "unpriced_rows": 0, "fresh_tokens": 5, "total_tokens": 5, "estimated_cost_usd": 0.01, "model_lanes": [{"model": "gpt-5.5"}]},
        "touched_files": files,
    }


def test_projection_unions_session_touched_files_into_actions() -> None:
    projection = build_task_projection([_session_with_touched(["src/edited_by_hook.py"])], [])
    task = projection["tasks"][0]
    assert "src/edited_by_hook.py" in task["actions"]["touched_files"]


def test_projection_unions_hook_and_section_files() -> None:
    # A section reports one file; the hook captured another. Both reach Actions.
    session = _session_with_touched(["src/hook_path.py"])
    work_item = {
        "client": "hermes",
        "client_session_id": "sess-1",
        "section_id": "w1",
        "run_id": "sess-1",
        "latest_status": "completed",
        "files": ["src/section_path.py"],
    }
    projection = build_task_projection([session], [work_item])
    touched = projection["tasks"][0]["actions"]["touched_files"]
    assert "src/hook_path.py" in touched
    assert "src/section_path.py" in touched
