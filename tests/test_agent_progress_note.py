"""The agent-reported goal and progress note on a recorded section.

A closed section carries `progress`: what got done, in words a reader with no
background can follow, ending with a clause that says where the work stopped.
Measured basis (2026-09-20 blind trials, six real tasks): without that clause,
readers took unfinished work as concluded on 6 of 6 tasks; with it, on 0 of 6.

Each rule is pinned in both directions -- the refusal and the acceptance -- and
across every write lane, because the MCP, HTTP and CLI lanes share one rule.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentacct.api import create_local_api_app
from agentacct.cli import app
from agentacct.mcp import TOOLS, SentinelMCPServer
from agentacct.semantic_rules import (
    GOAL_MAX_CHARACTERS,
    PROGRESS_MAX_CHARACTERS,
    SemanticRecordError,
    require_progress_note,
    stopping_clause,
)
from agentacct.service import SentinelService
from agentacct.work_ledger import build_work_ledger

SUMMARY = "Added the retry guard to the checkout client and covered it with two tests."
PROGRESS = "Checkout retries now reuse the first charge. Stopped before the refund path; next: cover it."


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


def _section(server: SentinelMCPServer, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "source": "codex",
        "section_id": "progress-section",
        "section_status": "started",
        "section_title": "Stop duplicate checkout charges",
        "client_session_id": "session-progress",
    }
    arguments.update(overrides)
    return _call(server, "agentacct_record_section", arguments)


# --- the rule itself ----------------------------------------------------------


@pytest.mark.parametrize("status", ["completed", "handed_off"])
def test_closing_without_progress_is_refused_with_a_fix(status: str) -> None:
    with pytest.raises(SemanticRecordError) as refusal:
        require_progress_note(status, progress=None, section_id="s1", source="codex")
    message = str(refusal.value)
    assert f"section_status={status} requires `progress`" in message
    assert 'section_id="s1"' in message and "progress=" in message


@pytest.mark.parametrize("status", ["started", "checkpoint", "blocked"])
def test_other_statuses_do_not_require_progress(status: str) -> None:
    require_progress_note(status, progress=None)
    require_progress_note(status, progress="   ")


@pytest.mark.parametrize(
    "progress",
    [
        "Retries reuse the first charge id. Done.",
        "Retries reuse the first charge id. Done; next: open the PR.",
        "Retries reuse the first charge id. Stopped before the refund path.",
        "Retries reuse the first charge id; next: cover refunds.",
        "Retries reuse the first charge id. Blocked on the staging key.",
        "Retries reuse the first charge id. Handed off to the next session.",
        "Retries reuse the first charge id. Waiting for review.",
        "Retries reuse the first charge id. Next — cover refunds.",
        "Retries reuse the first charge id. \"Done.\"",
        "Retries reuse the first charge id.\nNext: cover refunds.",
        "Done: retries reuse the first charge id.",
    ],
)
def test_a_note_ending_where_the_work_stopped_is_accepted(progress: str) -> None:
    require_progress_note("completed", progress=progress)


@pytest.mark.parametrize(
    "progress",
    [
        # The outcome alone: exactly the note a stranger reads as "finished".
        "Retries now reuse the first charge id everywhere.",
        # A stopping word that is not where the note ends.
        "Done with the client. Retries now reuse the first charge id.",
        # Negative control: a stopping word inside a longer word is not one.
        "Retries reuse the first charge id. Next.js now builds the page.",
        "Retries reuse the first charge id. Nextly the page builds.",
        "Retries reuse the first charge id. Doneness is unclear.",
    ],
)
def test_a_note_that_does_not_say_where_it_stopped_is_refused(progress: str) -> None:
    with pytest.raises(SemanticRecordError, match="must end with a clause that says where the work stopped"):
        require_progress_note("completed", progress=progress)


def test_the_stopping_rule_applies_whenever_progress_is_sent() -> None:
    with pytest.raises(SemanticRecordError, match="where the work stopped"):
        require_progress_note("started", progress="Reading the checkout client before changing it.")


def test_progress_length_is_bounded_in_both_directions() -> None:
    with pytest.raises(SemanticRecordError, match="received 5"):
        require_progress_note("completed", progress="Done.")
    too_long = "x" * PROGRESS_MAX_CHARACTERS + ". Done."
    with pytest.raises(SemanticRecordError, match=f"received {len(too_long)}"):
        require_progress_note("completed", progress=too_long)
    at_cap = "x" * (PROGRESS_MAX_CHARACTERS - len(". Done.")) + ". Done."
    require_progress_note("completed", progress=at_cap)


def test_goal_is_optional_and_capped() -> None:
    require_progress_note("started", progress=None, goal=None)
    require_progress_note("started", progress=None, goal="g" * GOAL_MAX_CHARACTERS)
    with pytest.raises(SemanticRecordError, match=f"at most {GOAL_MAX_CHARACTERS}"):
        require_progress_note("started", progress=None, goal="g" * (GOAL_MAX_CHARACTERS + 1))


def test_stopping_clause_is_the_last_readable_clause() -> None:
    assert stopping_clause("Built it. Stopped after tests; next: open the PR.") == "next: open the PR."
    assert stopping_clause("Built it. Done.  ") == "Done."
    assert stopping_clause("Built it... ") == "Built it..."


# --- the MCP lane -------------------------------------------------------------


def test_the_tool_schema_describes_goal_and_progress() -> None:
    schema = next(tool for tool in TOOLS if tool["name"] == "agentacct_record_section")["inputSchema"]
    properties = schema["properties"]
    assert properties["goal"]["maxLength"] == GOAL_MAX_CHARACTERS
    assert properties["progress"]["maxLength"] == PROGRESS_MAX_CHARACTERS
    assert "where" in properties["progress"]["description"] and "stopped" in properties["progress"]["description"]
    assert "progress" not in schema["required"]


def test_mcp_refuses_a_completed_section_without_progress_and_accepts_the_resend(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    refused = _error(_section(server, section_status="completed", summary=SUMMARY))
    assert refused is not None and "requires `progress`" in refused

    stored = _stored(_section(server, section_status="completed", summary=SUMMARY, progress=PROGRESS))
    metadata = stored["event"]["metadata"]
    assert metadata["progress"] == PROGRESS
    assert metadata["summary"] == SUMMARY


def test_the_summary_rule_still_answers_first(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_status="completed", progress=PROGRESS))
    assert message is not None and "requires `summary`" in message


def test_mcp_refuses_an_over_cap_progress_with_both_numbers(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    too_long = "x" * PROGRESS_MAX_CHARACTERS + ". Done."
    message = _error(_section(server, section_status="completed", summary=SUMMARY, progress=too_long))
    assert message is not None
    assert str(PROGRESS_MAX_CHARACTERS) in message and str(len(too_long)) in message


def test_goal_is_first_wins_and_progress_is_newest_on_the_work_item(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, goal="Stop duplicate charges when a checkout retries"))
    _stored(
        _section(
            server,
            section_status="checkpoint",
            goal="A later goal that must not replace the first",
            progress="Found where retries mint a new charge id. Next: reuse the first one.",
        )
    )
    _stored(_section(server, section_status="completed", summary=SUMMARY, progress=PROGRESS))

    ledger = build_work_ledger(SentinelService(tmp_path / "state").list_all_events())
    [item] = [item for item in ledger["work_items"] if item.get("section_id") == "progress-section"]
    assert item["goal"] == "Stop duplicate charges when a checkout retries"
    assert item["progress"] == PROGRESS


def test_a_later_event_without_progress_keeps_the_last_note(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(
        _section(
            server,
            section_status="checkpoint",
            progress="Found where retries mint a new charge id. Next: reuse the first one.",
        )
    )
    _stored(_section(server, section_status="blocked", blocker="The staging payment key has expired."))

    ledger = build_work_ledger(SentinelService(tmp_path / "state").list_all_events())
    [item] = [item for item in ledger["work_items"] if item.get("section_id") == "progress-section"]
    assert item["progress"] == "Found where retries mint a new charge id. Next: reuse the first one."


def test_work_status_reads_the_note_back_to_the_agent(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(
        _section(
            server,
            section_status="checkpoint",
            goal="Stop duplicate charges when a checkout retries",
            progress="Found where retries mint a new charge id. Next: reuse the first one.",
        )
    )
    status = _stored(_call(server, "agentacct_work_status", {"client_session_id": "session-progress"}))
    [brief] = status["open_sections"]
    assert brief["goal"] == "Stop duplicate charges when a checkout retries"
    assert brief["progress"].endswith("Next: reuse the first one.")


# --- the HTTP and CLI lanes share the rule ------------------------------------


def _work_event(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "source": "codex",
        "event_kind": "section",
        "status": "completed",
        "section_id": "http-progress",
        "title": "Stop duplicate checkout charges",
        "summary": SUMMARY,
    }
    request.update(overrides)
    return request


def test_http_lane_refuses_a_completed_section_without_progress(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    refused = client.post("/work-events", json=_work_event())
    assert refused.status_code == 400, refused.text
    assert "requires `progress`" in refused.text

    accepted = client.post("/work-events", json=_work_event(progress=PROGRESS, objective="Stop duplicate charges"))
    assert accepted.status_code == 200, accepted.text
    payload = accepted.json()
    assert payload["work_event"]["progress"] == PROGRESS
    assert payload["v1_event"]["metadata"]["progress"] == PROGRESS

    ledger = build_work_ledger(SentinelService(tmp_path / "state").list_all_events())
    [item] = [item for item in ledger["work_items"] if item.get("section_id") == "http-progress"]
    assert item["goal"] == "Stop duplicate charges"
    assert item["progress"] == PROGRESS


def test_cli_lane_takes_progress_and_refuses_without_it(tmp_path) -> None:
    runner = CliRunner()
    store = tmp_path / "state"
    base = [
        "evidence", "work-event", "--store-dir", str(store), "--source", "codex", "--kind", "section",
        "--status", "completed", "--section-id", "cli-progress", "--title", "Stop duplicate checkout charges",
        "--summary", SUMMARY, "--json",
    ]
    refused = runner.invoke(app, base)
    assert refused.exit_code != 0
    assert "progress" in (refused.output + str(refused.exception))

    accepted = runner.invoke(app, [*base, "--progress", PROGRESS, "--goal", "Stop duplicate charges"])
    assert accepted.exit_code == 0, accepted.output
    payload = json.loads(accepted.output)
    assert payload["work_event"]["progress"] == PROGRESS
    assert payload["work_event"]["objective"] == "Stop duplicate charges"
