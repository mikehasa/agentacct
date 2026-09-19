"""The MCP recording contract an agent actually follows (round-2 K58-K63).

Each test replays what a real agent does: close a section without repeating
its title, checkpoint without repeating its kind, copy the refusal's example
call, read work_status without a session, and resume a handoff.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agentacct import install_guide
from agentacct.display_vocabulary import DECISION_DEFINITIONS
from agentacct.mcp import SentinelMCPServer
from agentacct.work_ledger import build_work_ledger

OUTCOME = "Moved the minus sign before the currency symbol in format_amount and added a regression test"


def _call(server: SentinelMCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )


def _error(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    return None if error is None else str(error.get("message"))


def _stored(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response.get("error")
    return json.loads(response["result"]["content"][0]["text"])


def _section(server: SentinelMCPServer, **arguments: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"source": "claude-code", "client_session_id": "sess-a"}
    base.update(arguments)
    return _call(server, "agentacct_record_section", {k: v for k, v in base.items() if v is not None})


def _item(server: SentinelMCPServer, section_id: str, session: str = "sess-a") -> dict[str, Any]:
    ledger = build_work_ledger(server.service.list_all_events())
    return next(
        item
        for item in ledger["work_items"]
        if item["section_id"] == section_id and item.get("client_session_id") == session
    )


def _parse_example_call(message: str) -> dict[str, Any]:
    match = re.search(r"agentacct_record_section\((.*)\)\.$", message)
    assert match, message
    body = match.group(1)
    parsed: dict[str, Any] = {
        key: re.sub(r"\\(.)", r"\1", value)
        for key, value in re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', body)
    }
    files = re.search(r'files=\[("(?:[^"\\]|\\.)*")\]', body)
    if files:
        parsed["files"] = [re.sub(r'^"|"$', "", files.group(1))]
    return parsed


# --- K58: identity fields are sticky ----------------------------------------


def test_closing_a_section_does_not_need_the_title_repeated(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, section_id="fmt", section_status="started", section_title="Fix sign placement", kind="implementation", files=["moneyutil/format.py"]))
    stored = _stored(_section(server, section_id="fmt", section_status="completed", summary=OUTCOME))
    assert stored["event"]["metadata"]["section_title"] == "Fix sign placement"
    assert stored["event"]["metadata"]["section_title_inherited"] is True
    assert _item(server, "fmt")["title"] == "Fix sign placement"


def test_title_is_still_required_on_a_sections_first_record(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_id="fresh", section_status="started"))
    assert message is not None and "section_title is required" in message


def test_title_is_not_borrowed_from_another_session_with_the_same_section_id(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, section_id="shared", section_status="started", section_title="Other session work"))
    message = _error(
        _section(server, client_session_id="sess-b", section_id="shared", section_status="completed", summary=OUTCOME)
    )
    assert message is not None and "section_title is required" in message


def test_the_latest_title_is_inherited_so_a_rename_sticks(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, section_id="fmt", section_status="started", section_title="First name", files=["moneyutil/format.py"]))
    _stored(_section(server, section_id="fmt", section_status="checkpoint", section_title="Renamed step"))
    stored = _stored(_section(server, section_id="fmt", section_status="completed", summary=OUTCOME))
    assert stored["event"]["metadata"]["section_title"] == "Renamed step"


def test_checkpoint_without_kind_keeps_the_declared_kind(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, section_id="rs", section_status="started", section_title="Research options", kind="research"))
    stored = _stored(_section(server, section_id="rs", section_status="checkpoint"))
    # D4: the kind is now inherited onto the record itself, not merely preserved
    # in the rollup. A call that omits it used to STORE kind="unknown", which
    # every per-event reader then believed.
    assert stored["event"]["metadata"]["kind"] == "research"
    assert stored["event"]["metadata"]["kind_inherited"] is True
    _stored(_section(server, section_id="rs", section_status="completed", summary=OUTCOME))
    assert _item(server, "rs")["kind"] == "research"


def test_a_legacy_unknown_kind_snapshot_never_overwrites_a_declared_kind(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, section_id="impl", section_status="started", section_title="Implement it", kind="implementation"))
    server.service.record_event(
        {
            "source": "claude-code",
            "event_type": "section_checkpoint",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": "claude-code",
                "client_session_id": "sess-a",
                "section_id": "impl",
                "section_status": "checkpoint",
                "section_title": "Implement it",
                "kind": "unknown",
            },
        },
        transport="http",
    )
    assert _item(server, "impl")["kind"] == "implementation"


def test_every_missing_field_is_named_in_one_refusal(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_id="both", section_status="completed"))
    assert message is not None
    assert "section_title is required" in message
    assert "requires `summary`" in message
    assert message.count("agentacct_record_section(") == 1


def test_the_http_lane_inherits_the_title_too(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, section_id="fmt", section_status="started", section_title="Fix sign placement", files=["moneyutil/format.py"]))
    recorded = server.service.record_event(
        {
            "source": "claude-code",
            "event_type": "section_completed",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": "claude-code",
                "client_session_id": "sess-a",
                "section_id": "fmt",
                "section_status": "completed", "files": ["src/agentacct/mcp.py"],
                "summary": OUTCOME,
            },
        },
        transport="http",
    )
    assert recorded["metadata"]["section_title"] == "Fix sign placement"


# --- K59: the refusal's example call round-trips -----------------------------


def test_refusal_example_replayed_is_accepted(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    long_title = 'Fix the "negative" sign placement in format_amount for every currency symbol we support'
    assert len(long_title) > 60
    message = _error(
        _section(server, section_id="fmt", section_status="completed", section_title=long_title)
    )
    assert message is not None
    example = _parse_example_call(message)
    assert example["source"] == "claude-code"
    assert example["section_title"] == long_title
    assert example["summary"].startswith("<")
    # Every placeholder the example carries is a SLOT, and an unfilled one is
    # refused again rather than stored as a fabricated path.
    assert example["files"] == ["<project-relative path this step changed>"]
    assert _error(_call(server, "agentacct_record_section", {**example, "client_session_id": "sess-a"})) is not None
    example["summary"] = OUTCOME
    example["files"] = ["moneyutil/format.py"]
    replay = _stored(_call(server, "agentacct_record_section", {**example, "client_session_id": "sess-a"}))
    assert replay["event"]["metadata"]["section_title"] == long_title
    assert replay["event"]["metadata"]["files"] == ["moneyutil/format.py"]


def test_refusal_for_a_missing_source_never_suggests_an_empty_one(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_call(server, "agentacct_record_section", {"source": "", "section_id": "x", "section_status": "completed"}))
    assert message is not None
    assert 'source=""' not in message


# --- K60: no write advice without a session ----------------------------------


def _unscoped_status(server: SentinelMCPServer, **arguments: Any) -> dict[str, Any]:
    return _stored(_call(server, "agentacct_work_status", arguments))


def test_unscoped_work_status_leads_with_scope_and_gives_no_write_advice(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, client_session_id="other", section_id="live", section_status="started", section_title="Someone else's live step", kind="implementation"))
    _stored(_section(server, client_session_id="other", section_id="done", section_status="completed", section_title="Unverified step", kind="implementation", summary=OUTCOME, files=["moneyutil/format.py"]))
    for arguments in ({}, {"section_id": "live"}, {"project_dir": "/tmp/nowhere"}):
        status = _unscoped_status(server, **arguments)
        advice = status["what_to_do_next"]
        assert advice[0] == (
            "No session is in scope; the sections below belong to other sessions and are shown "
            "read-only. Pass client_session_id to see your own work."
        )
        joined = " ".join(advice)
        assert "Close each open section" not in joined
        assert "agentacct_record_machine_check" not in joined
    status = _unscoped_status(server)
    rows = [*status["open_sections"], *status["completed_without_evidence"]]
    assert rows and all(row["read_only"] is True and row["owner_session"] == "other" for row in rows)
    assert [row["section_id"] for row in _unscoped_status(server, section_id="live")["open_sections"]] == ["live"]


def test_scoped_work_status_still_advises_its_own_session(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, section_id="live", section_status="started", section_title="My live step"))
    status = _stored(_call(server, "agentacct_work_status", {"client_session_id": "sess-a"}))
    assert any("Close each open section" in line for line in status["what_to_do_next"])
    assert "read_only" not in status["open_sections"][0]


# --- K61: handoffs can be found and resumed ----------------------------------


def _handoff(server: SentinelMCPServer, session: str, section_id: str, project_dir: str) -> None:
    _stored(_section(server, client_session_id=session, section_id=section_id, section_status="started", section_title="Subtract amounts", project_dir=project_dir, files=["moneyutil/core.py"]))
    _stored(
        _section(
            server,
            client_session_id=session,
            section_id=section_id,
            section_status="handed_off",
            summary="Subtraction is done; formatting negative amounts remains for the next session",
            next_step="Put the minus sign before the currency symbol in format_amount",
            project_dir=project_dir,
        )
    )


def test_work_status_lists_the_sessions_own_handoff_with_its_next_step(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _handoff(server, "sess-h", "subtract", str(tmp_path / "proj"))
    status = _stored(_call(server, "agentacct_work_status", {"client_session_id": "sess-h"}))
    assert status["counts"]["handed_off"] == 1
    (row,) = status["handed_off_sections"]
    assert row["section_id"] == "subtract"
    assert row["next_step"] == "Put the minus sign before the currency symbol in format_amount"
    assert row["owner_session"] == "sess-h"
    assert row["read_only"] is False
    assert row["updated_at"]


def test_a_resuming_session_finds_other_sessions_handoffs_read_only(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    project = str(tmp_path / "proj")
    _handoff(server, "sess-h", "subtract", project)
    _handoff(server, "sess-x", "elsewhere", str(tmp_path / "other-proj"))
    status = _stored(_call(server, "agentacct_work_status", {"client_session_id": "sess-new", "project_dir": project}))
    assert [(row["section_id"], row["read_only"]) for row in status["handed_off_sections"]] == [("subtract", True)]
    unscoped = _unscoped_status(server, project_dir=project)
    assert [row["section_id"] for row in unscoped["handed_off_sections"]] == ["subtract"]
    assert all(row["read_only"] for row in unscoped["handed_off_sections"])


def test_a_handoff_finished_later_under_the_same_section_id_stops_listing(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    project = str(tmp_path / "proj")
    _handoff(server, "sess-h", "subtract", project)
    _stored(_section(server, client_session_id="sess-new", section_id="subtract", section_status="started", section_title="Resume subtract", project_dir=project, files=["moneyutil/format.py"]))
    _stored(_section(server, client_session_id="sess-new", section_id="subtract", section_status="completed", summary=OUTCOME, project_dir=project))
    assert _unscoped_status(server, project_dir=project)["handed_off_sections"] == []


def test_handoff_statement_claims_no_pickup() -> None:
    statement = DECISION_DEFINITIONS["handed_off"]
    assert statement.startswith("The agent handed this work off")
    assert statement.endswith("Picked up by: not recorded.")
    assert "not a completed or verified outcome" in statement
    assert "continued elsewhere" not in statement


# --- K63: one skip rule, and the supersession rule at the tool layer ---------


def test_server_instructions_state_one_skip_rule_and_the_supersession_rule() -> None:
    text = install_guide.MCP_SERVER_INSTRUCTIONS
    assert text.count("skip only") == 1
    assert "beats a gap" not in text
    assert "Re-run a check with the same `command` and `section_id` (or the same `check_key`) so a pass supersedes the earlier failure" in text


def test_handoff_marker_line_is_rendered_by_the_vocabulary() -> None:
    from agentacct.display_vocabulary import handoff_marker_line

    statement = DECISION_DEFINITIONS["handed_off"]
    assert handoff_marker_line(True, statement) == f"Handed off · {statement}"
    assert handoff_marker_line(False, None) == "Not the current handoff frontier · No handoff statement supplied"
