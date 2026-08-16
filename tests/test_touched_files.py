"""Hook-captured touched files (Actions dimension).

A file-EDIT tool's destination path is captured by the tool-activity hook and
represented in its least-revealing relative form — under the cwd it is cwd-relative
(``src/foo.py``); a file under the user's HOME is ``~``-relative (``~/.claude/x``, no
username); anything else out-of-tree is a cwd-relative ``../`` path. An absolute
prefix, the home dir, and the username are NEVER stored (never the content/diff/args
either). The path is spooled, drained onto the tool_activity_observed event,
aggregated per session, and unioned into the Task's Actions touched files alongside
the agent-reported MCP section/check files.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

import agentacct.hooks as hooks
from agentacct.hooks import _edit_target_relpath, capture_tool_activity
from agentacct.task_projection import build_task_projection
from agentacct.tool_activity import (
    _normalize_touched_path,
    build_touched_files_by_session,
    drain_tool_activity_spool,
    record_tool_activity_tick,
)

# The real _user_home_abs, captured before the autouse fixture below patches the module
# attribute — so a test can exercise the genuine HOME-resolution guard.
_REAL_USER_HOME_ABS = hooks._user_home_abs


@pytest.fixture(autouse=True)
def _fixed_home(monkeypatch: pytest.MonkeyPatch) -> str:
    # Pin the user's home so relativization is deterministic regardless of the runner's
    # real home (otherwise a runner whose home contained a test path would reclassify
    # it, e.g. a /Users/u/... target under a /Users/u home). Disjoint from the
    # /Users/u paths used by the out-of-tree cases below.
    monkeypatch.setattr(hooks, "_user_home_abs", lambda: "/Users/owner")
    return "/Users/owner"


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


def test_relpath_in_tree_is_cwd_relative() -> None:
    assert _edit_target_relpath(_edit_event("/Users/u/repo/src/foo.py"), "edit") == "src/foo.py"


def test_relpath_keeps_already_relative_path() -> None:
    assert _edit_target_relpath(_edit_event("src/bar.py"), "edit") == "src/bar.py"


def test_relpath_out_of_tree_non_home_is_dotdot() -> None:
    # A file edited OUTSIDE the tree and NOT under home rides as a ``../`` path (Actions
    # spans out-of-tree edits) — the absolute prefix never survives.
    assert _edit_target_relpath(_edit_event("/etc/passwd"), "edit") == "../../../etc/passwd"
    assert _edit_target_relpath(_edit_event("/srv/data/x.py", cwd="/opt/app"), "edit") == "../../srv/data/x.py"


def test_relpath_home_file_is_tilde_relative() -> None:
    # A file under the user's HOME is ``~``-relative — captured WITHOUT the username,
    # whether the cwd is under home (../ would strip it) or a sibling of it.
    assert (
        _edit_target_relpath(_edit_event("/Users/owner/.claude/settings.json", cwd="/Users/owner/repo"), "edit")
        == "~/.claude/settings.json"
    )
    assert _edit_target_relpath(_edit_event("/Users/owner/other/x.py", cwd="/Users/owner/repo"), "edit") == "~/other/x.py"


def test_relpath_out_of_home_cwd_home_file_never_leaks_username() -> None:
    # THE RE-DESCENT LEAK CASE: cwd OUTSIDE home editing a home file. A plain
    # os.path.relpath would climb above home and back down through /Users/owner/...,
    # embedding the username as ordinary relative segments; the ``~`` form does not.
    got = _edit_target_relpath(_edit_event("/Users/owner/.claude/settings.json", cwd="/private/tmp/build"), "edit")
    assert got == "~/.claude/settings.json"
    assert "owner" not in got


def test_relpath_cwd_at_or_above_home_uses_tilde_no_username() -> None:
    # cwd AT or ABOVE home (a daemon/container cwd of "/" or "/Users") editing a home
    # file: the cwd-relative form would descend through /Users/owner/ and store the
    # username, so the ``~`` form must win. HOME is pinned to /Users/owner by the fixture.
    assert (
        _edit_target_relpath(_edit_event("/Users/owner/.claude/settings.json", cwd="/"), "edit")
        == "~/.claude/settings.json"
    )
    assert _edit_target_relpath(_edit_event("/Users/owner/.ssh/id_rsa", cwd="/Users"), "edit") == "~/.ssh/id_rsa"
    # a NON-home file from cwd="/" is still a safe cwd-relative path (no username).
    assert _edit_target_relpath(_edit_event("/etc/hosts", cwd="/"), "edit") == "etc/hosts"


def test_relpath_absolute_home_without_cwd_is_tilde() -> None:
    # An absolute home path with no cwd is still safe to capture as ``~/…`` (no username).
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "/Users/owner/.zshrc"}}
    assert _edit_target_relpath(ev, "edit") == "~/.zshrc"


def test_relpath_drops_absolute_without_cwd_not_under_home() -> None:
    # An absolute path we cannot relativize (no cwd) and NOT under home is dropped —
    # an absolute prefix is never stored.
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "/x/y.py"}}
    assert _edit_target_relpath(ev, "edit") is None


def test_user_home_abs_rejects_degenerate_home(monkeypatch: pytest.MonkeyPatch) -> None:
    # A home that is unset/unresolvable ("~"), relative, or the filesystem ROOT ("/",
    # what expanduser yields for HOME="" or HOME="/") is not a usable privacy boundary
    # and must resolve to None (so the caller drops rather than leaks).
    for raw in ("/", "", "~", "relative/home"):
        monkeypatch.setattr(os.path, "expanduser", lambda p, _r=raw: _r if p == "~" else p)
        assert _REAL_USER_HOME_ABS() is None, f"expected None for expanduser->{raw!r}"
    monkeypatch.setattr(os.path, "expanduser", lambda p: "/Users/owner" if p == "~" else p)
    assert _REAL_USER_HOME_ABS() == "/Users/owner"


def test_cwd_may_hide_home() -> None:
    # The filesystem root and well-known home parents can hide a home when HOME is
    # unknown; a normal project dir (even at depth 1) cannot.
    assert hooks._cwd_may_hide_home("/") is True
    assert hooks._cwd_may_hide_home("/Users") is True
    assert hooks._cwd_may_hide_home("/home") is True
    assert hooks._cwd_may_hide_home("/app") is False
    assert hooks._cwd_may_hide_home("/workspace") is False
    assert hooks._cwd_may_hide_home("/Users/owner") is False


def test_relpath_drops_out_of_tree_when_home_undetectable(monkeypatch: pytest.MonkeyPatch) -> None:
    # When home cannot be determined safely (_user_home_abs -> None, e.g. a degenerate
    # HOME of "/" or an unresolvable/relative HOME), an out-of-tree edit is DROPPED —
    # step 3 must not emit a ``../`` path that could re-descend through /Users/<user>/
    # (nor a ``~/Users/<user>/`` path from a root home). In-tree edits are unaffected.
    monkeypatch.setattr(hooks, "_user_home_abs", lambda: None)
    assert _edit_target_relpath(_edit_event("/Users/owner/.claude/x", cwd="/private/tmp/build"), "edit") is None
    assert _edit_target_relpath(_edit_event("/etc/hosts", cwd="/opt/app"), "edit") is None
    # in-tree still works with no resolvable home (a DEEP, non-shallow cwd)
    assert _edit_target_relpath(_edit_event("/Users/u/repo/src/a.py"), "edit") == "src/a.py"


def test_relpath_home_parent_cwd_dropped_but_project_cwd_kept_when_home_undetectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With home UNKNOWN, an in-tree edit from a home-PARENT cwd ("/", "/Users", "/home")
    # is DROPPED (its cwd-relative form would retain /Users/<user>/). A normal project
    # cwd ("/app", "/workspace") — even at depth 1 — still captures in-tree edits, so the
    # feature is not silently lost in containerized CI (Cloud Build/Tekton /workspace,
    # Docker WORKDIR /app) that also has a degenerate HOME.
    monkeypatch.setattr(hooks, "_user_home_abs", lambda: None)
    assert _edit_target_relpath(_edit_event("/Users/owner/repo/src/a.py", cwd="/"), "edit") is None
    assert _edit_target_relpath(_edit_event("/Users/owner/.ssh/id_rsa", cwd="/Users"), "edit") is None
    assert _edit_target_relpath(_edit_event("/home/u/proj/x.py", cwd="/home"), "edit") is None
    assert _edit_target_relpath(_edit_event("/app/src/main.py", cwd="/app"), "edit") == "src/main.py"
    assert _edit_target_relpath(_edit_event("/workspace/src/main.py", cwd="/workspace"), "edit") == "src/main.py"
    # a deep project cwd also still captures
    assert _edit_target_relpath(_edit_event("/workspace/repo/src/a.py", cwd="/workspace/repo"), "edit") == "src/a.py"


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


def test_normalize_rejects_absolute_and_drive() -> None:
    assert _normalize_touched_path("/etc/passwd") is None
    assert _normalize_touched_path("C:\\Users\\x") is None
    assert _normalize_touched_path("\\\\server\\share") is None  # UNC (double backslash)
    assert _normalize_touched_path("\\Users\\bob\\secret\\id_rsa") is None  # single-backslash Windows absolute
    assert _normalize_touched_path("a\x00b") is None


def test_normalize_keeps_dotdot_and_tilde() -> None:
    # A ``../`` escape and a ``~/…`` home path carry no absolute prefix, so both are
    # KEPT (only absolute/drive/UNC paths are rejected).
    assert _normalize_touched_path("../../other/file.py") == "../../other/file.py"
    assert _normalize_touched_path("./../sib/x.py") == "../sib/x.py"
    assert _normalize_touched_path("~/.claude/settings.json") == "~/.claude/settings.json"


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


def test_capture_records_out_of_tree_edit_as_relative() -> None:
    # An absolute edit OUTSIDE the cwd (and not under home) is captured as a ``../``
    # relative touched path — the out-of-repo file is no longer silently dropped.
    store = _store()
    capture_tool_activity(json.dumps(_edit_event("/Users/u/other-project/mod.py")), store_dir=store, client="claude-code")
    md = drain_tool_activity_spool(store)[0]["metadata"]
    assert md["touched_files"] == ["../other-project/mod.py"]


def test_capture_records_home_edit_as_tilde_no_username() -> None:
    # End-to-end: an out-of-home cwd editing a HOME file stores ``~/…`` — the username
    # never lands in the spooled touched path (the re-descent leak is closed).
    store = _store()
    capture_tool_activity(
        json.dumps(_edit_event("/Users/owner/.claude/settings.json", cwd="/private/tmp/build")),
        store_dir=store,
        client="claude-code",
    )
    md = drain_tool_activity_spool(store)[0]["metadata"]
    assert md["touched_files"] == ["~/.claude/settings.json"]
    assert not any("owner" in path for path in md["touched_files"])


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


def test_projection_binds_out_of_tree_and_home_paths_into_actions() -> None:
    # A ``../`` out-of-tree path and a ``~/…`` home path flow UNCHANGED from the hook
    # into the Actions dimension — no downstream gate drops them (the projection unions
    # session touched files without re-normalizing).
    projection = build_task_projection([_session_with_touched(["../sibling/mod.py", "~/.claude/x"])], [])
    touched = projection["tasks"][0]["actions"]["touched_files"]
    assert "../sibling/mod.py" in touched
    assert "~/.claude/x" in touched


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
