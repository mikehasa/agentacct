"""Fuzz and cross-lane agreement tests for the recording rules.

Three questions this file answers, none of which the hand-written tests settle:

1. **Do the validators survive hostile input?** A validator that raises
   ``TypeError`` instead of ``InvalidParams`` turns a bad report into a broken
   tool call, and an MCP client reports that as a transport failure, not as
   guidance the agent can act on.
2. **Is the normalization deterministic and idempotent?** The same bytes must
   produce the same stored record every time, and normalizing an already-stored
   value must not change it again -- otherwise a replay or a re-import drifts.
3. **Do the write lanes agree?** The metadata budget is already shared across
   MCP, HTTP and CLI because measuring it in one lane only "broke the symmetry
   the shared budget exists for". The display rules repeat that risk, so the
   agreement is measured here rather than assumed.

The generators are a fixed-seed deterministic fuzzer rather than Hypothesis:
the project has no property-testing dependency, and a seeded corpus keeps a
failure reproducible from the test output alone.
"""

from __future__ import annotations

import json
import random
import string
import unicodedata

import pytest

from agentacct.mcp import (
    InvalidParams,
    SentinelMCPServer,
    _collapse_display_text,
    _collapse_narrative_text,
    _display_title,
    _narrative_text,
)
from agentacct.semantic_rules import SemanticRecordError, require_terminal_outcome

# --- deterministic corpus ---------------------------------------------------

HOSTILE_TEXT = [
    "",
    " ",
    "\t",
    "\n",
    "\r\n",
    "\x00",
    "\x00\x01\x02",
    "   \t \n  ",
    "---",
    "...",
    "-",
    "0",
    "a",
    "ab",
    "\u00a0\u00a0",                     # non-breaking spaces only
    "\u200b\u200b",                     # zero-width spaces only
    "café",
    "日本語のテキストです",
    "🎉🎉",
    "👩‍👩‍👧‍👦 family",
    "\u0301\u0301",                     # combining marks only
    "e\u0301",                          # decomposed accent
    "\ufeffBOM at start",
    "a" * 1199,
    "a" * 1200,
    "a" * 1201,
    "a" * 5000,
    "line1\nline2",
    "line1\r\nline2",
    "line1\n\n\n\nline2",
    "- bullet\n- bullet",
    "1. one\n2. two",
    "tab\tseparated\tvalues",
    "  leading and trailing  ",
    "internal   runs    of     spaces",
    "<script>alert(1)</script>",
    "**bold** and `code` and # heading",
    "| a | b |\n| - | - |",
    "emoji + text 🚀 done",
    "RTL \u202eoverride",
    "null\u0000inside",
    "very " * 300,
    "x" * 160,
    "x" * 161,
]


def _corpus(seed: int = 20260912, size: int = 240) -> list[str]:
    """HOSTILE_TEXT plus seeded random strings, including raw bytes-ish noise."""
    rng = random.Random(seed)
    alphabet = string.ascii_letters + string.digits + " \t\n-_/.:,;()[]{}\"'`*#|\\\x00\x1f\u00a0\u2028"
    generated = [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))
        for _ in range(size)
    ]
    generated += [
        "".join(chr(rng.randint(1, 0x2FFF)) for _ in range(rng.randint(0, 60)))
        for _ in range(60)
    ]
    return HOSTILE_TEXT + generated


# --- 1. validators must reject, never crash --------------------------------


@pytest.mark.parametrize("value", _corpus())
def test_display_title_either_returns_readable_text_or_raises_invalid_params(value: str) -> None:
    """The contract: a title either renders, or the caller gets InvalidParams.

    Never a TypeError, never a ValueError escaping as a transport failure, and
    never a value that would render as nothing.
    """
    try:
        result = _display_title({"section_title": value}, "section_title", max_length=160)
    except InvalidParams:
        return  # a refusal the agent can act on
    assert result is not None
    # Whatever survived must be single-line, control-free, and non-blank.
    assert result == result.strip()
    assert result
    assert "\n" not in result and "\t" not in result and "\r" not in result
    assert not any(unicodedata.category(character) == "Cc" for character in result)
    assert len(result) <= 160
    # At least two characters are alphanumeric OR letter-like: `isalnum()` is
    # False for scripts such as Hangul jamo and some combining forms, which are
    # perfectly readable text, so the check is deliberately not `isalnum() >= 2`.
    assert sum(1 for character in result if character.isalnum() or unicodedata.category(character).startswith("L")) >= 2


@pytest.mark.parametrize("value", _corpus())
def test_narrative_text_never_crashes_and_preserves_line_structure(value: str) -> None:
    try:
        result = _narrative_text({"summary": value}, "summary", max_length=1200)
    except InvalidParams:
        return  # over the field cap: a refusal, not a crash
    if result is None:
        return  # nothing readable was supplied
    assert len(result) <= 1200
    assert result == result.strip()
    # Line breaks survive so a renderer can show paragraphs and lists.
    assert result.count("\n\n\n") == 0, "blank-line runs must collapse"


@pytest.mark.parametrize("value", _corpus())
def test_collapse_functions_are_deterministic_and_idempotent(value: str) -> None:
    """Same bytes -> same stored value, and re-normalizing is a no-op.

    Idempotence is what makes a replay or a re-import safe: a record that has
    already been through the normalizer must not change when it goes through
    again.
    """
    first = _collapse_display_text(value)
    second = _collapse_display_text(value)
    assert first == second
    assert _collapse_display_text(first) == first

    narrative_first = _collapse_narrative_text(value)
    assert narrative_first == _collapse_narrative_text(value)
    assert _collapse_narrative_text(narrative_first) == narrative_first


@pytest.mark.parametrize("status", ["completed", "handed_off", "blocked", "started", "checkpoint"])
@pytest.mark.parametrize("summary", [None, "", "   ", "short", "x" * 39, "x" * 40, "x" * 1200])
@pytest.mark.parametrize("blocker", [None, "", "tiny", "x" * 20])
def test_terminal_outcome_gate_is_total(status: str, summary, blocker) -> None:
    """Every combination either passes or raises InvalidParams -- no other exit.

    A gate with an unhandled branch is how a rule becomes an outage.
    """
    try:
        require_terminal_outcome(
            status, summary=summary, blocker=blocker,
            section_id="s", source="codex", title="T",
        )
    except (InvalidParams, SemanticRecordError):
        return


# --- 2. the write path survives the corpus ---------------------------------


@pytest.mark.parametrize("value", _corpus(seed=7, size=40)[:120])
def test_record_section_never_returns_a_transport_error(tmp_path, value: str) -> None:
    """Any text is recorded or refused -- never a crash."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_section",
                "arguments": {
                    "source": "codex",
                    "section_id": "fuzz",
                    "section_status": "completed",
                    "section_title": value[:200] or "Fallback title",
                    "summary": value[:1200],
                },
            },
        }
    )
    assert "error" not in response or response["error"]["code"] == -32602, response.get("error")
    assert response.get("result") is not None or response.get("error") is not None


@pytest.mark.parametrize("value", _corpus(seed=11, size=40)[:120])
def test_record_machine_check_never_returns_a_transport_error(tmp_path, value: str) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_machine_check",
                "arguments": {
                    "source": "codex",
                    "name": value[:80] or "check",
                    "result": "passed",
                    "evidence_type": "test",
                    "command": value[:500],
                    "summary": value[:1200],
                },
            },
        }
    )
    assert "error" not in response or response["error"]["code"] == -32602, response.get("error")


def test_stored_value_is_stable_across_a_second_write(tmp_path) -> None:
    """Writing the same report twice must store the same normalized text.

    The idempotency key makes the second call a replay; without a stable
    normalization the stored bytes could differ between them.
    """
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    arguments = {
        "source": "codex",
        "section_id": "stable",
        "section_status": "completed",
        "section_title": "  Padded   title\twith\tcontrol ",
        "summary": "Outcome first: the migration ran.\n\n\n\n- detail one\n- detail two",
        "idempotency_key": "stable-1",
    }
    first = json.loads(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "agentacct_record_section", "arguments": arguments}}
        )["result"]["content"][0]["text"]
    )["event"]["metadata"]
    assert first["section_title"] == "Padded title with control"
    assert first["summary"] == "Outcome first: the migration ran.\n\n- detail one\n- detail two"

    # Re-normalizing the stored values must be a no-op.
    assert _collapse_display_text(first["section_title"]) == first["section_title"]
    assert _collapse_narrative_text(first["summary"]) == first["summary"]


# --- 3. lane agreement ------------------------------------------------------


def _mcp_verdict(tmp_path, tool: str, arguments: dict) -> tuple[bool, str]:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    )
    if "error" in response:
        return False, str(response["error"]["message"])
    return True, ""


def test_mcp_is_never_more_permissive_than_the_http_model(tmp_path) -> None:
    """The shared budget exists so the same payload gets the same verdict on
    every lane. The direction that matters for data quality is this one: whatever
    the HTTP model refuses, the MCP lane must refuse too.

    (MCP may legitimately be STRICTER -- the display and completeness rules live
    in its handler. The reverse gap is pinned separately below.)
    """
    from agentacct.api import WorkEventRecordRequest
    from pydantic import ValidationError

    cases = [
        {"title": "a" * 5000},                             # over the 240 title cap
        {"title": "Valid title", "summary": "x" * 5000},    # over the 1200 summary cap
        {"title": "Valid title", "status": "completed"},    # the normal case
        {"title": "Valid title", "status": "started"},
        {"title": "", "status": "started"},                 # empty title
    ]
    for extra in cases:
        payload = {"source": "codex", "event_kind": "section", **extra}
        try:
            WorkEventRecordRequest(**payload)
            http_ok = True
        except ValidationError:
            http_ok = False

        arguments: dict = {
            "source": "codex",
            "section_id": "lane",
            "section_status": extra.get("status", "started"),
            "section_title": extra.get("title", "Valid title"),
        }
        if extra.get("summary") is not None:
            arguments["summary"] = extra["summary"]
        if arguments["section_status"] in {"completed", "handed_off"} and not arguments.get("summary"):
            arguments["summary"] = "A real outcome summary for the lane agreement case."
        mcp_ok, message = _mcp_verdict(tmp_path, "agentacct_record_section", arguments)

        if not http_ok:
            assert not mcp_ok, f"MCP accepted what HTTP rejects: {extra}"


def test_every_lane_refuses_the_same_incomplete_records(tmp_path) -> None:
    """The rules live at the shared choke point, so no lane can store what
    another refuses.

    This replaces the earlier test that PINNED the gap (completeness rules in the
    MCP handler only, HTTP and CLI free to store what MCP refused). That gap is
    closed: `SentinelService.record_event` -- the single writer every lane funnels
    through -- applies the same rules, so the divergence cannot come back without
    failing here.
    """
    from agentacct.service import SentinelService

    incomplete_section = {
        "source": "codex",
        "event_type": "section_completed",
        "metadata": {
            "sentinel_semantic_kind": "section",
            "section_id": "no-outcome",
            "section_status": "completed",
            "section_title": "Finished work with no outcome recorded",
        },
    }
    incomplete_check = {
        "source": "codex",
        "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence",
            "name": "check",
            "result": "passed",
        },
    }

    for transport in ("http", "cli", "mcp"):
        service = SentinelService(tmp_path / f"store-{transport}")
        for label, event in (("section", incomplete_section), ("check", incomplete_check)):
            with pytest.raises(ValueError) as raised:
                service.record_event(dict(event), transport=transport)
            message = str(raised.value)
            assert "requires `summary`" in message or "too generic" in message, (transport, label, message)

    # And the MCP tool itself refuses the same two records, which is what makes
    # the refusal reach an agent as actionable JSON-RPC guidance.
    for label, tool, arguments in (
        ("section", "agentacct_record_section",
         {"source": "codex", "section_id": "no-outcome", "section_status": "completed",
          "section_title": "Finished work with no outcome recorded"}),
        ("check", "agentacct_record_machine_check",
         {"source": "codex", "name": "check", "result": "passed"}),
    ):
        ok, _ = _mcp_verdict(tmp_path, tool, arguments)
        assert not ok, label


def test_complete_records_are_accepted_on_every_lane(tmp_path) -> None:
    """The other direction: the gate must not become an outage."""
    from agentacct.service import SentinelService

    complete_section = {
        "source": "codex",
        "event_type": "section_completed",
        "metadata": {
            "sentinel_semantic_kind": "section",
            "section_id": "has-outcome",
            "section_status": "completed",
            "section_title": "Add a rate limiter to the login endpoint",
            "summary": "Added a 5-per-minute limiter and covered it with three tests.",
            "files": ["src/login.py"],
        },
    }
    complete_check = {
        "source": "codex",
        "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence",
            "name": "pytest tests/test_login.py",
            "result": "passed",
            "command": "pytest tests/test_login.py",
            "exit_code": 0,
        },
    }
    for transport in ("http", "cli", "mcp"):
        service = SentinelService(tmp_path / f"ok-{transport}")
        service.record_event(dict(complete_section), transport=transport)
        service.record_event(dict(complete_check), transport=transport)


def test_machine_recorded_events_are_not_subject_to_agent_rules(tmp_path) -> None:
    """Imported facts are out of scope by design: no agent authored them, so a
    refusal would drop a fact rather than correct a report."""
    from agentacct.service import SentinelService

    service = SentinelService(tmp_path / "store")
    service.record_event(
        {
            "source": "claude-code-local-session-import",
            "event_type": "model_usage",
            "metadata": {"sentinel_semantic_kind": "usage", "client": "claude-code", "input_tokens": 10},
        }
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "section_completed",
            # A machine-recorded section event with no authoring agent.
            "metadata": {"sentinel_semantic_kind": "section", "section_id": "imported", "section_status": "completed"},
        },
        transport="internal",
    )
    assert len(service.list_all_events()) == 2
