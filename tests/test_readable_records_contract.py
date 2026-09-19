"""The contract that stops an agent from manufacturing an unreadable record.

Round-4 group D. The proven defect this file pins: a well-behaved agent produced
TWO byte-identical check cards, both named ``python -m pytest
tests/test_percent.py``, both with no usable summary. It was COMPLYING -- the
tool said the safest stable identifier was the command, and said a summary was
optional -- so the fix is to the contract, not to the agent.

Every rule is tested in BOTH directions: the refusal for the record a reader
cannot act on, AND the acceptance of the shape an honest agent sends. The
invariants at the end are the ones a forward-only change must not break:
``check_identity`` stays stable across commits, and a row already in the store
whose NAME IS THE COMMAND keeps superseding exactly as it always did.
"""

from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from typing import Any

from agentacct.mcp import SentinelMCPServer
from agentacct.receipt import check_title
from agentacct.work_ledger import build_evidence_events


def _call(server: SentinelMCPServer, name: str, arguments: dict[str, Any], msg_id: int = 1) -> dict[str, Any]:
    return server.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )


def _error(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    return None if error is None else str(error.get("message"))


def _stored(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response.get("error")
    return json.loads(response["result"]["content"][0]["text"])


def _section(server: SentinelMCPServer, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "source": "claude-code",
        "client_session_id": "sess-d",
        "section_id": "percent",
        "section_status": "started",
        "section_title": "Round percentages half-up",
    }
    arguments.update(overrides)
    return _call(server, "agentacct_record_section", {k: v for k, v in arguments.items() if v is not None})


def _check(server: SentinelMCPServer, msg_id: int = 1, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "source": "claude-code",
        "client_session_id": "sess-d",
        "section_id": "percent",
        "name": "percentage() rounds half-up",
        "evidence_type": "test",
        "result": "passed",
        "command": "python -m pytest tests/test_percent.py",
        "exit_code": 0,
    }
    arguments.update(overrides)
    return _call(
        server,
        "agentacct_record_machine_check",
        {k: v for k, v in arguments.items() if v is not None},
        msg_id=msg_id,
    )


def _identity(server: SentinelMCPServer, event_id: str) -> str:
    events = build_evidence_events(server.service.list_all_events())
    return next(event["check_identity"] for event in events if event["event_id"] == event_id)


# --- D1: identity is not the display name -----------------------------------


def test_the_name_field_has_no_default_and_a_check_that_identifies_nothing_is_refused(tmp_path) -> None:
    """``name`` carries no schema default, so an omitted label is absent rather
    than a placeholder the agent never wrote. Dropping the default must not make
    a nameless, pointerless check legal."""

    from agentacct.mcp import TOOLS

    schema = next(tool for tool in TOOLS if tool["name"] == "agentacct_record_machine_check")
    assert "default" not in schema["inputSchema"]["properties"]["name"]

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_check(server, name=None, command=None, exit_code=None))
    assert message is not None
    assert "no `name`" in message and "command" in message


def test_the_name_field_is_described_as_a_label_beside_a_command(tmp_path) -> None:
    from agentacct.mcp import TOOLS

    schema = next(tool for tool in TOOLS if tool["name"] == "agentacct_record_machine_check")
    name = schema["inputSchema"]["properties"]["name"]["description"]
    assert "not the command" in name
    assert 'name="percentage() rounds half-up"' in name
    assert 'command="python -m pytest tests/test_percent.py"' in name
    # And it must no longer tell the agent that reusing the NAME is what keys
    # supersession -- the sentence that made the command the safest name.
    assert "Reusing the SAME name" not in name


def test_the_refusal_example_shows_a_prose_name_beside_a_command(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_check(server, name=None, command=None, exit_code=None))
    assert message is not None
    assert 'name="percentage() rounds half-up"' in message
    assert 'command="python -m pytest tests/test_percent.py"' in message
    # The old example put the command IN the name field, which is exactly what
    # the two identical cards were copied from.
    assert 'name="pytest tests/test_mcp.py"' not in message


def test_a_rewritten_label_does_not_split_a_check_from_its_earlier_run(tmp_path) -> None:
    """The point of D1: an honest agent rewords the label between runs. Keying
    identity on the command + type + section means the pass still lands on the
    same check as the failure."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, files=["src/percent.py"]))
    failed = _stored(
        _check(
            server,
            name="percentage() rounds half-up",
            result="failed",
            exit_code=1,
            summary="percentage(1, 3) returned 33.33, expected 33.34 (assert round_half_up failed)",
        )
    )["event"]["event_id"]
    passed = _stored(
        _check(
            server,
            msg_id=2,
            name="half-up rounding now matches the spec",  # reworded on purpose
            result="passed",
            exit_code=0,
        )
    )["event"]["event_id"]
    assert _identity(server, failed) == _identity(server, passed)


def test_check_key_groups_runs_whose_command_legitimately_varies(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    first = _stored(
        _check(server, check_key="percent-rounding", command="python -m pytest -k half_up_1")
    )["event"]["event_id"]
    second = _stored(
        _check(server, msg_id=2, check_key="percent-rounding", command="python -m pytest -k half_up_2")
    )["event"]["event_id"]
    assert _identity(server, first) == _identity(server, second)
    # And a different key is a different check, even on the same command.
    third = _stored(
        _check(server, msg_id=3, check_key="percent-formatting", command="python -m pytest -k half_up_1")
    )["event"]["event_id"]
    assert _identity(server, third) != _identity(server, first)


def test_the_same_command_in_a_different_section_is_a_different_check(tmp_path) -> None:
    """Section scope is what supersession already enforces pairwise, so the
    derived key carries it too: a suite re-run inside a later step must not
    silently retire the earlier step's failure."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    here = _stored(_check(server, section_id="percent"))["event"]["event_id"]
    there = _stored(_check(server, msg_id=2, section_id="format"))["event"]["event_id"]
    assert _identity(server, here) != _identity(server, there)


# --- D1 invariant: nothing already in the store moves ------------------------


def _legacy_identity(evidence_type: str, name: str, command: str) -> str:
    """The identity formula as it stood BEFORE this change, spelled out here so
    the test fails if the stored value ever moves rather than quietly agreeing
    with whatever the code now computes."""

    material = "\0".join((evidence_type, name, command))
    return f"check:{sha256(material.encode('utf-8')).hexdigest()[:16]}"


def test_a_stored_row_whose_name_is_the_command_keeps_its_identity(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    # Exactly the shape of task_7bb028c1: the command in the name field, no
    # `command` of its own.
    server.service.record_event(
        {
            "source": "claude-code",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "claude-code",
                "client_session_id": "sess-legacy",
                "section_id": "percent",
                "evidence_type": "test",
                "name": "python -m pytest tests/test_percent.py",
                "result": "passed",
                "exit_code": 0,
            },
        }
    )
    event = build_evidence_events(server.service.list_all_events())[0]
    assert event["check_identity"] == _legacy_identity("test", "python -m pytest tests/test_percent.py", "")
    assert event["check_identity_basis"] == "name_or_command"
    assert event["check_identity_stable"] is True


def test_two_legacy_rows_still_supersede_each_other_exactly_as_before(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    name = "python -m pytest tests/test_percent.py"
    for index, (result, exit_code, summary) in enumerate(
        (
            ("failed", 1, "2 failed: percentage(1, 3) returned 33.33, expected 33.34"),
            ("passed", 0, None),
        )
    ):
        metadata = {
            "sentinel_semantic_kind": "evidence",
            "client": "claude-code",
            "client_session_id": "sess-legacy",
            "section_id": "percent",
            "evidence_type": "test",
            "name": name,
            # The legacy rows carry the command too, which is what the pairwise
            # supersession gate matches on.
            "command": name,
            "result": result,
            "exit_code": exit_code,
        }
        if summary:
            metadata["summary"] = summary
        server.service.record_event(
            {
                "source": "claude-code",
                "created_at": 100.0 + index,
                "event_type": "machine_check",
                "metadata": metadata,
            }
        )
    events = build_evidence_events(server.service.list_all_events())
    failure = next(event for event in events if event["result"] == "failed")
    assert failure["supersession_state"] == "superseded"
    # One identity, so the two runs are still two runs of ONE check.
    assert len({event["check_identity"] for event in events}) == 1


def test_check_identity_is_unchanged_by_the_commit_it_ran_at(tmp_path) -> None:
    """A re-run at a new commit must still supersede the same check's earlier
    failure, so the server-captured revision is deliberately not in the key."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    first = _stored(_check(server, project_dir=str(repo)))
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "two"], cwd=repo, check=True)
    second = _stored(_check(server, msg_id=2, project_dir=str(repo)))

    assert first["event"]["metadata"]["git_commit"] != second["event"]["metadata"]["git_commit"]
    assert _identity(server, first["event"]["event_id"]) == _identity(server, second["event"]["event_id"])


# --- D2(a): a failure must describe itself -----------------------------------


def test_a_failed_check_with_no_summary_is_refused_with_the_reason(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_check(server, result="failed", exit_code=1, summary=None))
    assert message is not None
    assert "a reviewer cannot act on a failure with no description" in message
    assert "observed vs expected" in message


def test_an_error_check_with_no_summary_is_refused_too(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_check(server, result="error", exit_code=4, summary=None))
    assert message is not None and "a check recorded as error requires `summary`" in message


def test_a_terse_failure_description_is_accepted_as_the_agent_wrote_it(tmp_path) -> None:
    """Presence is the rule. 'got 3, want 4' is a complete observed-vs-expected
    statement, and a character count cannot tell it from filler."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_check(server, result="failed", exit_code=1, summary="got 3, want 4"))
    assert stored["event"]["metadata"]["summary"] == "got 3, want 4"


def test_a_real_failure_description_is_accepted(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(
        _check(
            server,
            result="failed",
            exit_code=1,
            summary="percentage(1, 3) returned 33.33, expected 33.34 (assert round_half_up failed)",
        )
    )
    assert stored["event"]["metadata"]["result"] == "failed"
    assert stored["event"]["metadata"]["summary"].startswith("percentage(1, 3) returned")


def test_a_passing_check_still_needs_no_summary(tmp_path) -> None:
    """The rule is about failures. Making every pass carry prose would be a new
    cost on the shape agents get right."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_check(server, summary=None))
    assert stored["event"]["metadata"].get("summary") in (None, "")


# --- D2(b): a stopped section must say where to resume -----------------------


def test_handed_off_without_a_next_step_is_refused(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(
        _section(
            server,
            section_status="handed_off",
            summary="Subtraction is done; formatting negative amounts remains for the next session.",
            files=["src/percent.py"],
        )
    )
    assert message is not None
    assert "requires `next_step`" in message
    assert "whoever picks the work up" in message


def test_blocked_without_a_next_step_is_refused_even_with_a_blocker(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(
        _section(
            server,
            section_status="blocked",
            blocker="The staging database rejects the migration without an owner role.",
            files=["src/percent.py"],
        )
    )
    assert message is not None
    assert "requires `next_step`" in message
    assert "what would unblock it" in message


def test_a_stopped_section_with_a_next_step_is_accepted(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(
        _section(
            server,
            section_status="handed_off",
            summary="Subtraction is done; formatting negative amounts remains for the next session.",
            next_step="Put the minus sign before the currency symbol in format_amount.",
            files=["src/percent.py"],
        )
    )
    assert stored["event"]["metadata"]["next_step"].startswith("Put the minus sign")


def test_completed_needs_no_next_step(tmp_path) -> None:
    """Only a STOPPED section owes a continuation point; a finished one does
    not, and asking for one would invite invented follow-ups."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(
        _section(
            server,
            section_status="completed",
            summary="Rounded each line item half-up and covered the boundary with two tests.",
            files=["src/percent.py"],
        )
    )
    assert "next_step" not in stored["event"]["metadata"]


# --- D2(c): a terminal section must anchor what it changed -------------------


def test_a_terminal_section_with_no_files_is_refused_and_names_the_escape(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(
        _section(
            server,
            section_status="completed",
            kind="implementation",
            summary="Rounded each line item half-up and covered the boundary with two tests.",
        )
    )
    assert message is not None
    assert "requires `files`" in message
    assert "only per-step anchor" in message
    # The escape must be NAMED, so an agent whose step touched nothing is never
    # pushed into inventing a path.
    assert "docs, planning, research, review" in message
    assert "never invent a path" in message


def test_a_file_free_kind_closes_without_files(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    for kind in ("review", "research", "planning", "docs"):
        stored = _stored(
            _section(
                server,
                section_id=f"read-{kind}",
                section_title=f"Read the {kind} material",
                section_status="completed",
                kind=kind,
                summary="Read the surrounding code and decided the rounding belongs in one helper.",
            )
        )
        assert stored["event"]["metadata"]["kind"] == kind


def test_naming_the_files_once_closes_the_section_later(tmp_path) -> None:
    """Sticky, like the title: a status report must not have to repeat the
    section's own facts, and repeating them is how a closing call ends up
    contradicting the opening one."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, kind="implementation", files=["src/percent.py"]))
    stored = _stored(
        _section(
            server,
            section_status="completed",
            summary="Rounded each line item half-up and covered the boundary with two tests.",
        )
    )
    # Inherited for the RULE only: the closing record must not claim paths it
    # did not send.
    assert "files" not in stored["event"]["metadata"]


def test_files_named_in_another_session_do_not_close_this_one(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, client_session_id="sess-other", kind="implementation", files=["src/percent.py"]))
    message = _error(
        _section(
            server,
            client_session_id="sess-mine",
            section_status="completed",
            kind="implementation",
            section_title="Round percentages half-up",
            summary="Rounded each line item half-up and covered the boundary with two tests.",
        )
    )
    assert message is not None and "requires `files`" in message


def test_the_example_placeholder_is_a_slot_not_a_path(tmp_path) -> None:
    """Copying the refusal's example verbatim must not store `<...>` as a real
    path: an unfilled slot is nothing supplied, and the same refusal returns."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(
        _section(
            server,
            section_status="completed",
            kind="implementation",
            summary="Rounded each line item half-up and covered the boundary with two tests.",
            files=["<project-relative path this step changed>"],
        )
    )
    assert message is not None and "requires `files`" in message


def test_every_missing_terminal_field_arrives_in_one_call(tmp_path) -> None:
    """The one-call refusal shape, extended to the new rules. Refusing one field
    per round trip turns closing a section into several retries, and an agent
    that gives up leaves a stale open step on the receipt."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_status="handed_off", kind="implementation"))
    assert message is not None
    assert "requires `summary`" in message
    assert "requires `next_step`" in message
    assert "requires `files`" in message
    assert message.count("agentacct_record_section(") == 1


# --- D2 invariant: a refusal rejects the CALL, never a stored record ---------


def test_a_refused_call_stores_nothing_and_leaves_earlier_records_intact(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, kind="implementation", files=["src/percent.py"]))
    before = list(server.service.list_all_events())
    for refused in (
        _section(server, section_id="other", section_title="Other step", section_status="completed",
                 kind="implementation", summary="Rounded each line item and covered the boundary."),
        _check(server, section_id="other", result="failed", exit_code=1, summary=None),
    ):
        assert _error(refused) is not None
    after = list(server.service.list_all_events())
    assert after == before


# --- D3: ranked, capped advisories -------------------------------------------


def test_a_duplicate_check_name_in_one_section_is_advised(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_check(server))
    payload = _stored(_check(server, msg_id=2, command="python -m pytest tests/test_percent.py -k half_up"))
    codes = [advisory["code"] for advisory in payload["advisories"]]
    assert "duplicate_check_name_in_section" in codes
    hint = next(a["hint"] for a in payload["advisories"] if a["code"] == "duplicate_check_name_in_section")
    assert "supersedes_check_event_id" in hint and "check_key" in hint


def test_a_check_with_no_command_and_no_artifact_is_advised(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(_check(server, command=None, files=["src/percent.py"]))
    codes = [advisory["code"] for advisory in payload["advisories"]]
    assert codes == ["check_without_command_or_artifact"]


def test_a_checkpoint_with_no_next_step_is_advised_never_refused(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(_section(server, section_status="checkpoint"))
    codes = [advisory["code"] for advisory in payload["advisories"]]
    assert codes == ["checkpoint_without_next_step"]
    assert payload["event"]["metadata"]["section_status"] == "checkpoint"


def test_a_checkpoint_that_says_where_it_stands_is_not_advised(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(_section(server, section_status="checkpoint", next_step="Run the boundary tests next."))
    assert "advisories" not in payload


def test_at_most_two_advisories_come_back_worst_for_the_reader_first() -> None:
    """The measured failure: a whole recorded session returned exactly ONE
    advisory -- a 6-character card-title overrun -- while the same session
    shipped two indistinguishable checks and a check with no command."""

    from agentacct.display_budget import CARD_TITLE_CHARACTERS
    from agentacct.semantic_rules import ADVISORY_RESPONSE_LIMIT, check_advisories

    advisories = check_advisories(
        name="x" * (CARD_TITLE_CHARACTERS + 20),
        result="failed",
        exit_code=0,
        command=None,
        duplicate_name_in_section=True,
    )
    assert len(advisories) == ADVISORY_RESPONSE_LIMIT == 2
    assert [advisory["code"] for advisory in advisories] == [
        "duplicate_check_name_in_section",
        "check_without_command_or_artifact",
    ]
    # The cosmetic note can never be the only thing an agent hears when a real
    # one is queued behind it.
    assert "title_over_card_budget" not in {advisory["code"] for advisory in advisories}


def test_a_long_title_alone_is_still_advised() -> None:
    from agentacct.display_budget import CARD_TITLE_CHARACTERS
    from agentacct.semantic_rules import check_advisories

    advisories = check_advisories(
        name="x" * (CARD_TITLE_CHARACTERS + 20),
        result="passed",
        exit_code=0,
        command="pytest -q",
    )
    assert [advisory["code"] for advisory in advisories] == ["title_over_card_budget"]


# --- D4: sticky kind, and a word for "could not reproduce" -------------------


def test_a_terminal_call_that_omits_kind_keeps_the_declared_one(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, kind="implementation", files=["src/percent.py"]))
    stored = _stored(
        _section(
            server,
            section_status="completed",
            summary="Rounded each line item half-up and covered the boundary with two tests.",
        )
    )
    assert stored["event"]["metadata"]["kind"] == "implementation"
    assert stored["event"]["metadata"]["kind_inherited"] is True


def test_a_supplied_kind_always_wins_over_the_inherited_one(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _stored(_section(server, kind="research"))
    stored = _stored(
        _section(
            server,
            section_status="completed",
            kind="implementation",
            files=["src/percent.py"],
            summary="Rounded each line item half-up and covered the boundary with two tests.",
        )
    )
    assert stored["event"]["metadata"]["kind"] == "implementation"
    assert "kind_inherited" not in stored["event"]["metadata"]


def test_not_reproduced_is_accepted_and_is_never_a_finding(tmp_path) -> None:
    """task_c5bffb80's clean investigation ("Could NOT reproduce...", exit 0)
    set the whole task's coral Finding badge, because `failed` was the only word
    the enum offered for "I looked and found nothing"."""

    from agentacct.display_vocabulary import (
        FAILED_CHECK_RESULTS,
        check_result_label,
        check_result_tone,
    )

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(
        _check(
            server,
            result="not_reproduced",
            exit_code=0,
            summary="Ran the reported sequence 20 times against a clean store; the duplicate row never appeared.",
        )
    )
    assert stored["event"]["metadata"]["result"] == "not_reproduced"
    event = build_evidence_events(server.service.list_all_events())[0]
    assert event["result"] == "not_reproduced"
    assert "not_reproduced" not in FAILED_CHECK_RESULTS
    assert check_result_label("not_reproduced") == "Could not reproduce"
    assert check_result_tone("not_reproduced") == "not_run"


def test_failed_with_exit_code_zero_is_pointed_at_not_reproduced(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(
        _check(
            server,
            result="failed",
            exit_code=0,
            summary="Could not reproduce the reported duplicate row in 20 clean runs.",
        )
    )
    hint = next(a["hint"] for a in payload["advisories"] if a["code"] == "failed_with_exit_code_zero")
    assert "record not_reproduced" in hint


def test_a_check_identified_only_by_its_command_is_titled_by_that_command() -> None:
    """The write rule and the renderer must agree about what identifies a check.

    ``require_check_identity`` tolerates a missing `name` when a `command` is
    supplied -- "something else says what ran". The renderer used to drop past
    the command to the evidence type, so that tolerated call produced a card
    titled "test": the unidentifiable card the rule exists to prevent, waved
    through by the rule itself.
    """

    check = {"name": None, "command": "python -m pytest -q tests/test_parse.py", "evidence_type": "test"}
    assert check_title(check) == "python -m pytest -q tests/test_parse.py"


def test_a_redacted_command_is_not_borrowed_as_a_title() -> None:
    """A redacted command is not the agent's text to print, so the card falls
    through to the evidence type rather than showing a command that was
    deliberately withheld."""

    check = {
        "name": None,
        "command": "deploy --token hunter2",
        "command_redacted": True,
        "evidence_type": "smoke",
    }
    assert check_title(check) == "smoke"


def test_a_name_still_outranks_the_command_in_the_title() -> None:
    check = {
        "name": "percentage() rounds half-up",
        "command": "python -m pytest tests/test_percent.py",
        "evidence_type": "test",
    }
    assert check_title(check) == "percentage() rounds half-up"
