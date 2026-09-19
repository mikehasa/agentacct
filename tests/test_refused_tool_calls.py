"""Refuse-before-dispatch: a tool call the USER DECLINED becomes its own
distinct, additive receipt signal — never folded into or subtracted from
executed tool counts, never on the evidence/outcome tiers, and impossible for
the agent-under-test to forge (the decline text is host-written).

Three layers are covered: the tool_activity event builder + reducer; the Claude
transcript matcher (anchored, with the honesty negatives); and the receipt
Actions-dimension surfacing.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentacct.client_usage import discover_client_usage_with_diagnostics
from agentacct.receipt import SOURCE_TRANSCRIPT_SCAN, _actions_dimension
from agentacct.tool_activity import (
    DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS,
    REFUSED_TOOL_CALL_CONTRACT_KEY,
    REFUSED_TOOL_CALL_EVENT_TYPE,
    TOOL_ACTIVITY_EVENT_TYPE,
    build_refused_actions_by_session,
    build_refused_tool_call_event,
    build_tool_activity_by_session,
    is_refused_tool_call_event,
    is_trusted_refused_tool_call_event,
)

# The exact text Claude Code writes when a user declines a tool's permission
# prompt. Hardcoded (not imported) so the test pins the REAL host wording.
_DENIAL = "The user doesn't want to proceed with this tool use. The tool use was rejected."


# --- layer 1: the event builder + reducer ------------------------------------


def test_build_refused_event_shape_and_none_guards() -> None:
    assert build_refused_tool_call_event(client="claude-code", session_id="s1", refused_action_count=0, captured_at=1.0) is None
    assert build_refused_tool_call_event(client="", session_id="s1", refused_action_count=2, captured_at=1.0) is None
    assert build_refused_tool_call_event(client="claude-code", session_id="", refused_action_count=2, captured_at=1.0) is None
    event = build_refused_tool_call_event(client="claude-code", session_id="s1", refused_action_count=3, captured_at=1.0)
    assert event is not None
    assert event["event_type"] == REFUSED_TOOL_CALL_EVENT_TYPE
    assert event["metadata"]["refused_action_count"] == 3
    assert event["metadata"]["capture_basis"] == DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS
    assert is_refused_tool_call_event(event)
    assert is_trusted_refused_tool_call_event(event)  # the builder stamps the contract
    # Stable event_id per (client, session): a re-import replaces, never doubles.
    again = build_refused_tool_call_event(client="claude-code", session_id="s1", refused_action_count=9, captured_at=99.0)
    assert again["event_id"] == event["event_id"]
    # Carries no arguments / paths / free text — only the count (+ the trust stamp).
    assert set(event["metadata"]) == {
        "client",
        "client_session_id",
        "capture_basis",
        "captured_at",
        "sentinel_semantic_kind",
        "refused_action_count",
        REFUSED_TOOL_CALL_CONTRACT_KEY,
    }


def test_reducer_sums_per_session_and_ignores_other_events() -> None:
    events = [
        build_refused_tool_call_event(client="claude-code", session_id="s1", refused_action_count=2, captured_at=1.0),
        {  # an executed tool-activity event must be ignored by the refusal reducer
            "event_type": TOOL_ACTIVITY_EVENT_TYPE,
            "metadata": {"client": "claude-code", "client_session_id": "s1", "tool_category_counts": {"execute": 5}},
        },
        {  # a malformed / non-positive count is dropped, never a fabricated zero
            "event_type": REFUSED_TOOL_CALL_EVENT_TYPE,
            "metadata": {"client": "claude-code", "client_session_id": "s2", "refused_action_count": 0},
        },
    ]
    assert build_refused_actions_by_session(events) == {("claude-code", "s1"): 2}


def test_executed_reducer_ignores_refused_events() -> None:
    # The whole reason for a distinct event_type: a refusal can never be summed
    # into (or supersede) the executed tool-category counts.
    refused = build_refused_tool_call_event(client="claude-code", session_id="s1", refused_action_count=4, captured_at=1.0)
    assert build_tool_activity_by_session([refused]) == {}


def test_reducer_ignores_a_refused_event_without_the_trust_stamp() -> None:
    # An event of the right TYPE but missing the reserved contract key (what the
    # generic record_event lane strips from a forgery) must never count.
    forged = {
        "event_type": REFUSED_TOOL_CALL_EVENT_TYPE,
        "metadata": {"client": "claude-code", "client_session_id": "s1", "refused_action_count": 99},
    }
    assert not is_trusted_refused_tool_call_event(forged)
    assert build_refused_actions_by_session([forged]) == {}


def test_forged_refused_via_record_event_is_stripped_not_trusted(tmp_path: Path) -> None:
    """An agent cannot mint a 'user denied N actions' signal with one MCP write.

    A raw record_event caller that stamps the reserved contract itself has it
    stripped and tombstoned, so the reducer never counts the forgery — the same
    defense worksets and finding dispositions use.
    """
    from agentacct.service import SentinelService

    service = SentinelService(tmp_path)
    service.record_event(
        {
            "source": "claude-code",
            "event_type": REFUSED_TOOL_CALL_EVENT_TYPE,
            "run_id": None,
            "metadata": {
                "client": "claude-code",
                "client_session_id": "evil-session",
                "refused_action_count": 99,
                REFUSED_TOOL_CALL_CONTRACT_KEY: 1,  # forged trust stamp
            },
        }
    )
    events = service.list_all_events()
    # The forgery never trusts: the reducer counts nothing.
    assert build_refused_actions_by_session(events) == {}
    # The audit row survives, but the reserved contract is gone and tombstoned.
    row = next(
        e for e in events
        if isinstance(e.get("metadata"), dict) and e["metadata"].get("client_session_id") == "evil-session"
    )
    assert REFUSED_TOOL_CALL_CONTRACT_KEY not in row["metadata"]
    assert row["metadata"].get("reserved_refused_tool_call_provenance_stripped") is True


def test_trusted_emit_path_preserves_the_stamp_and_counts(tmp_path: Path) -> None:
    """The import lane (replace_events) must PRESERVE the contract stamp through
    the store, or the whole feature would silently count zero in production."""
    from agentacct.service import SentinelService

    service = SentinelService(tmp_path)
    event = build_refused_tool_call_event(
        client="claude-code", session_id="real-session", refused_action_count=2, captured_at=1.0
    )
    # Same call the cli import-local emit loop makes for refused events.
    service.replace_events(is_refused_tool_call_event, [event])
    events = service.list_all_events()
    stored = next(
        e for e in events
        if isinstance(e.get("metadata"), dict) and e["metadata"].get("client_session_id") == "real-session"
    )
    assert REFUSED_TOOL_CALL_CONTRACT_KEY in stored["metadata"]  # stamp survived the trusted lane
    assert build_refused_actions_by_session(events) == {("claude-code", "real-session"): 2}


# --- layer 2: the Claude transcript matcher (anchored + negatives) -----------


def _write_claude_session(claude_home: Path, session_id: str, lines: list[dict]) -> None:
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _usage_line(session_id: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "cwd": "/work/project",
        "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 30, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
    }


def _tool_use_line(session_id: str, tool_use_id: str, name: str = "Bash") -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}]},
    }


def _tool_result_line(session_id: str, tool_use_id: str, *, text: str, is_error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": [{"type": "text", "text": text}]}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "sessionId": session_id, "message": {"role": "user", "content": [block]}}


def _observation(claude_home: Path):
    result = discover_client_usage_with_diagnostics(client="claude-code", claude_home=claude_home)
    assert len(result.session_observations) == 1
    return result


def test_user_denied_tool_call_is_counted(tmp_path: Path) -> None:
    sid = "c037bd88-0000-4000-8000-000000000001"
    _write_claude_session(
        tmp_path / "home",
        sid,
        [
            _usage_line(sid),
            _tool_use_line(sid, "toolu_1"),
            _tool_result_line(sid, "toolu_1", text=_DENIAL, is_error=True),
        ],
    )
    result = _observation(tmp_path / "home")
    assert result.session_observations[0].refused_action_count == 1
    # Executed usage is untouched — the refusal rode alongside a real usage event.
    assert result.events and result.events[0].client_session_id == sid


def test_ordinary_tool_error_is_not_a_refusal(tmp_path: Path) -> None:
    # is_error alone is a normal tool FAILURE (the tool ran) — not a user denial.
    sid = "c037bd88-0000-4000-8000-000000000002"
    _write_claude_session(
        tmp_path / "home",
        sid,
        [
            _usage_line(sid),
            _tool_use_line(sid, "toolu_1"),
            _tool_result_line(sid, "toolu_1", text="Error: command exited with status 1", is_error=True),
        ],
    )
    assert _observation(tmp_path / "home").session_observations[0].refused_action_count == 0


def test_accepted_tool_result_is_not_a_refusal(tmp_path: Path) -> None:
    sid = "c037bd88-0000-4000-8000-000000000003"
    _write_claude_session(
        tmp_path / "home",
        sid,
        [
            _usage_line(sid),
            _tool_use_line(sid, "toolu_1"),
            _tool_result_line(sid, "toolu_1", text="total 8\n-rw-r--r-- 1 x y"),
        ],
    )
    assert _observation(tmp_path / "home").session_observations[0].refused_action_count == 0


def test_denial_for_unknown_tool_use_id_is_not_counted(tmp_path: Path) -> None:
    # A denial-shaped tool_result whose id was never a real tool_use must not
    # trigger a count (guards against a stray/free-text id).
    sid = "c037bd88-0000-4000-8000-000000000004"
    _write_claude_session(
        tmp_path / "home",
        sid,
        [
            _usage_line(sid),
            _tool_result_line(sid, "toolu_ghost", text=_DENIAL, is_error=True),
        ],
    )
    assert _observation(tmp_path / "home").session_observations[0].refused_action_count == 0


# --- layer 3: the receipt Actions dimension ----------------------------------


def _task(refused: int, *, category_counts: dict | None = None) -> dict:
    actions = {
        "tool_category_counts": category_counts or {"execute": 3},
        "tool_category_total": sum((category_counts or {"execute": 3}).values()),
        "touched_files": [],
        "touched_file_count": 0,
        "refused_action_count": refused,
    }
    return {"actions": actions}


def test_actions_dimension_surfaces_refused_count_and_gap() -> None:
    dim = _actions_dimension(_task(2))
    assert dim["refused_action_count"] == 2
    assert any("2 actions refused" in g and "user denied" in g for g in dim["gaps"])
    assert SOURCE_TRANSCRIPT_SCAN in dim["provenance"]
    # Additive: it never alters the executed tally.
    assert dim["tool_category_counts"] == {"execute": 3}
    assert dim["tool_category_total"] == 3


def test_actions_dimension_has_no_refusal_when_none() -> None:
    dim = _actions_dimension(_task(0))
    assert dim["refused_action_count"] == 0
    assert not any("refused" in g for g in dim["gaps"])
    # This signal does not, on its own, add transcript_scan provenance.
    assert SOURCE_TRANSCRIPT_SCAN not in dim["provenance"]
