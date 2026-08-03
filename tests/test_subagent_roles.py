"""Tests for the subagent role/task reader (Claude child transcripts)."""

from __future__ import annotations

import json
from pathlib import Path

from agentacct import subagent_roles as sr


def _write_transcript(root: Path, project: str, parent: str, agent_id: str, *, attribution, task_text, extra=None):
    directory = root / project / parent / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "summary", "summary": "meta line, skipped"},
        {"attributionAgent": attribution, "type": "assistant"} if attribution else {"type": "assistant"},
        # a tool_result user message (role user, but not text) must be skipped
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "echo"}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": task_text}]}},
    ]
    (directory / f"{agent_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines), encoding="utf-8"
    )


def test_read_subagent_role_from_transcript(tmp_path):
    root = tmp_path / "projects"
    parent, agent = "p-123", "agent-deadbeef"
    _write_transcript(root, "proj-A", parent, agent, attribution="Explore", task_text="Investigate the thing")
    role = sr.read_subagent_role(f"{parent}:{agent}", projects_root=root)
    assert role is not None
    assert role.agent_type == "Explore"
    assert role.task == "Investigate the thing"  # first user TEXT (tool_result skipped)


def test_read_subagent_role_non_child_and_missing(tmp_path):
    root = tmp_path / "projects"
    assert sr.read_subagent_role("plain-session-id", projects_root=root) is None  # no ':agent-'
    assert sr.read_subagent_role("p1:agent-missing", projects_root=root) is None  # no file


def test_subagent_role_scan_gate(tmp_path):
    # conftest pins AGENTACCT_SCAN_SUBAGENT_ROLES off. An explicit root bypasses
    # the gate; no root + scan off returns None (never reads the real machine).
    root = tmp_path / "projects"
    _write_transcript(root, "P", "par", "agent-x", attribution="Plan", task_text="t")
    assert sr.read_subagent_role("par:agent-x", projects_root=root).agent_type == "Plan"
    assert sr.read_subagent_role("par:agent-x") is None  # scan disabled, no explicit root


def test_read_roles_for_children_single_scan(tmp_path):
    root = tmp_path / "projects"
    parent = "par"
    _write_transcript(root, "P", parent, "agent-1", attribution="Explore", task_text="A")
    _write_transcript(root, "P", parent, "agent-2", attribution="general-purpose", task_text="B")
    children = [f"{parent}:agent-1", f"{parent}:agent-2", f"{parent}:agent-missing", "not-a-child"]
    roles = sr.read_roles_for_children(parent, children, projects_root=root)
    assert set(roles.keys()) == {f"{parent}:agent-1", f"{parent}:agent-2"}
    assert roles[f"{parent}:agent-1"].agent_type == "Explore"
    assert roles[f"{parent}:agent-2"].task == "B"


def test_read_subagent_role_survives_malformed(tmp_path):
    root = tmp_path / "projects"
    directory = root / "P" / "par" / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-x.jsonl").write_text(
        "not json\n"
        "[1,2,3]\n"
        '{"type":"user","message":{"role":"user","content":"hi there"}}\n',
        encoding="utf-8",
    )
    role = sr.read_subagent_role("par:agent-x", projects_root=root)
    assert role is not None
    assert role.agent_type is None  # no attributionAgent present
    assert role.task == "hi there"  # string content is accepted


def test_read_subagent_role_survives_binary(tmp_path):
    # A corrupted/binary transcript (or a torn multi-byte char in one still being
    # written) must not raise UnicodeDecodeError — the read is byte-bounded and
    # errors="replace". Valid lines before the garbage are still parsed.
    root = tmp_path / "projects"
    directory = root / "P" / "par" / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-x.jsonl").write_bytes(
        json.dumps({"attributionAgent": "Explore"}).encode() + b"\n\xff\xfe bad bytes \x80\n"
    )
    role = sr.read_subagent_role("par:agent-x", projects_root=root)  # must not raise
    assert role is not None and role.agent_type == "Explore"


def test_subagent_files_for_parent_rejects_traversal_and_metachars(tmp_path):
    root = tmp_path / "projects"
    directory = root / "proj" / "par" / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-1.jsonl").write_text("{}", encoding="utf-8")
    # a well-formed parent still resolves
    assert set(sr.subagent_files_for_parent("par", projects_root=root).keys()) == {"agent-1"}
    # '*' must NOT match every parent (glob.escape → literal)
    assert sr.subagent_files_for_parent("*", projects_root=root) == {}
    # traversal ids are rejected outright (never read outside the root)
    assert sr.subagent_files_for_parent("../../etc", projects_root=root) == {}
    assert sr.read_roles_for_children("../../x", ["../../x:agent-z"], projects_root=root) == {}


def test_subagent_files_for_parent_batches(tmp_path):
    root = tmp_path / "projects"
    _write_transcript(root, "P", "par", "agent-1", attribution="Explore", task_text="A")
    _write_transcript(root, "P", "par", "agent-2", attribution="Plan", task_text="B")
    files = sr.subagent_files_for_parent("par", projects_root=root)
    assert set(files.keys()) == {"agent-1", "agent-2"}
    assert sr.subagent_files_for_parent("nope", projects_root=root) == {}
