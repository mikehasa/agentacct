"""Display-quality and completeness rules for agent-recorded data.

Design: ``design-plans/data-quality/RULES.md``. Every rule here is enforced at
record time, because a record the UI cannot render is worse than a refusal the
agent can fix in one retry. The tests below pin both directions: the refusal for
an incomplete report, and the acceptance of every shape the real ledger actually
contains.

Measured basis (installed store, 8,369 events): 0 titles empty or over the field
cap, 5 of 536 terminal sections (0.9%) without a summary, 64 of 335 checks with
no command, 272 of 335 naming no files. The rules close those gaps without
refusing anything an agent legitimately records.
"""

from __future__ import annotations

import json
from typing import Any

from agentacct.mcp import SentinelMCPServer


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
        "source": "codex",
        "section_id": "rules-section",
        "section_status": "started",
        "section_title": "Enforce display rules",
    }
    arguments.update(overrides)
    return _call(server, "agentacct_record_section", arguments)


def _check(server: SentinelMCPServer, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "source": "codex",
        "name": "pytest tests/test_mcp.py",
        "result": "passed",
        # evidence_type is present in 335 of 335 stored checks, and it is what
        # keeps the call on the evidence lane rather than the run-scoped
        # before/after outcome lane.
        "evidence_type": "test",
        "command": "pytest tests/test_mcp.py",
    }
    arguments.update(overrides)
    return _call(server, "agentacct_record_machine_check", arguments)


# --- R1: a displayed title must be real text --------------------------------


def test_whitespace_only_title_is_refused_with_the_reason(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_title="   "))
    assert message is not None
    assert "readable text" in message and "whitespace" in message


def test_punctuation_only_title_is_refused(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_title="--- ..."))
    assert message is not None
    assert "at least 2 letters or digits" in message


def test_title_alias_is_held_to_the_same_rule(tmp_path) -> None:
    """`title` is the HTTP lane's name for the same field and must not be a
    loophole around the display rule."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_title=None, title="  "))
    assert message is not None
    assert "readable text" in message or "at least 2 letters" in message


# --- R2: control characters never reach display -----------------------------


def test_newline_and_tab_in_a_title_become_one_line(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_section(server, section_title="Fix\nUI\tspacing"))
    assert stored["event"]["metadata"]["section_title"] == "Fix UI spacing"


def test_collapsing_happens_before_the_length_check(tmp_path) -> None:
    """160 characters of title plus stray whitespace is 160 characters of title."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    padded = "  " + ("a" * 79) + "\t \n" + ("b" * 79) + "  "
    stored = _stored(_section(server, section_title=padded))
    collapsed = stored["event"]["metadata"]["section_title"]
    assert collapsed == ("a" * 79) + " " + ("b" * 79)
    assert len(collapsed) == 159


def test_a_genuinely_long_title_is_still_refused_with_both_numbers(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_title="a" * 200))
    assert message is not None
    assert "160" in message and "200" in message


def test_narrative_newlines_are_preserved(tmp_path) -> None:
    """Summaries keep real line structure: it is how a mangled tool call is
    detected, and how a reader sees a list."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    summary = "Ran the focused suite after the change.\n\n- 3 passed\n- 0 failed"
    stored = _stored(
        _section(
            server,
            section_status="completed",
            summary=summary,
        )
    )
    assert stored["event"]["metadata"]["summary"] == summary


# --- R4: a terminal section must carry its outcome --------------------------


def test_completed_section_without_a_summary_is_refused_with_a_fix(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_status="completed"))
    assert message is not None
    assert "summary" in message
    # The refusal must be actionable: it names the field and shows a corrected call.
    assert "agentacct_record_section(" in message
    assert "section_id=\"rules-section\"" in message


def test_completed_section_with_a_short_summary_is_refused(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_status="completed", summary="done"))
    assert message is not None
    assert "at least 40 characters" in message


def test_completed_section_with_a_real_summary_is_accepted(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(
        _section(
            server,
            section_status="completed",
            summary="Added the rate limiter to the login endpoint and covered it with three tests.",
        )
    )
    assert stored["event"]["metadata"]["section_status"] == "completed"


def test_blocked_section_requires_a_blocker_not_a_summary(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_status="blocked"))
    assert message is not None
    assert "blocker" in message
    stored = _stored(
        _section(
            server,
            section_status="blocked",
            blocker="The staging database rejects the migration without an owner role.",
            next_step="Ask the platform team to grant the owner role.",
        )
    )
    assert stored["event"]["metadata"]["blocker"].startswith("The staging database")


def test_started_section_needs_no_summary(tmp_path) -> None:
    """The rule must not make the normal opening call harder."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_section(server, section_status="started"))
    assert stored["event"]["metadata"]["section_status"] == "started"


# --- R5/R6: a machine check must be reproducible and identifiable -----------


def test_check_without_command_files_or_artifact_is_refused(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(
        _call(
            server,
            "agentacct_record_machine_check",
            {"source": "codex", "result": "passed", "evidence_type": "test", "name": "login smoke test"},
        )
    )
    assert message is not None
    assert "command" in message and "files" in message
    # And it names the honest alternative for a manual observation.
    assert "agentacct_record_event" in message


def test_check_with_only_a_command_is_accepted(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_check(server, command="pytest -q", files=None, name="pytest -q"))
    assert stored["event"]["metadata"]["command"] == "pytest -q"


def test_check_with_only_files_is_accepted(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_check(server, command=None, files=["tests/test_mcp.py"], name="test_mcp suite"))
    assert stored["event"]["metadata"]["files"] == ["tests/test_mcp.py"]


def test_files_naming_only_the_project_root_do_not_satisfy_the_rule(tmp_path) -> None:
    """'files: ["."]' survives validation but stores nothing, so it cannot be the
    evidence a check rests on."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_check(server, command=None, files=["."], name="root files check"))
    assert message is not None
    assert "command" in message


def test_before_after_outcome_lane_stays_supported(tmp_path) -> None:
    """A repair recorded as before/after exit codes is evidence and must not be
    refused for naming no command.

    The outcome lane additionally requires an existing run (the server resolves
    run_id="latest" against the store), so this call also carries the files the
    smoke check covered -- which is the shape the CLI writes when it knows them.
    """
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(
        _call(
            server,
            "agentacct_record_machine_check",
            {
                "source": "codex",
                "name": "smoke",
                "evidence_type": "smoke",
                "result": "passed",
                "files": ["scripts/smoke.sh"],
                "summary": "Recorded the before/after exit codes for the smoke script.",
            },
        )
    )
    assert "error" not in stored, stored.get("error")
    assert stored["event"]["metadata"]["files"] == ["scripts/smoke.sh"]


def test_generic_check_name_without_evidence_is_refused(tmp_path) -> None:
    """Supersession keys on the name, so a bare "check" that says nothing else
    cannot identify what would be superseded."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_call(server, "agentacct_record_machine_check", {"source": "codex", "name": "check", "result": "passed"}))
    assert message is not None
    # The refusal must name the field and show a usable example.
    assert "too generic" in message and "pytest tests/test_mcp.py" in message


def test_generic_name_with_a_command_is_accepted(tmp_path) -> None:
    """A command is the identity in practice: `pytest -q` says exactly what ran."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_check(server, name="check", command="pytest -q"))
    assert stored["event"]["metadata"]["name"] == "check"


# --- every shape the real store contains must still be accepted -------------


def test_real_ledger_shapes_are_accepted(tmp_path) -> None:
    """Regression guard against over-strictness: these mirror the measured
    records (median title 40 characters, median summary 207, checks named
    "pytest", "pnpm build:web", artifact checks with paths)."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    accepted = [
        _section(
            server,
            section_id="real-1",
            section_status="completed",
            section_title="Coordinate 100 native macOS design reviews",
            summary="Ran the ten review rounds and recorded each disposition with its source hash.",
        ),
        _section(
            server,
            section_id="real-2",
            section_status="completed",
            section_title="Evaluate masked native information candidates",
            summary="Compared the masked candidates against the measured ledger and kept the two that agreed.",
            files=["src/agentacct/mcp.py"],
        ),
        _check(server, name="pytest tests/test_mcp.py", command="pytest tests/test_mcp.py", section_id="real-1"),
        _check(
            server,
            name="pnpm build:web",
            command="pnpm build:web",
            evidence_type="build",
            section_id="real-1",
            files=["apps/web/src/main.tsx"],
        ),
    ]
    for response in accepted:
        assert "error" not in response, response.get("error")


def _server_with_a_run(tmp_path) -> tuple[SentinelMCPServer, str]:
    """A store with one real run.

    The before/after summaries put a call on the run-scoped OUTCOME lane, which
    resolves run_id="latest" against the store. That lane is not what these two
    tests are about -- they are about whether a blank summary counts as evidence
    -- so the run exists purely to let the call reach the rule.
    """
    from agentacct.runner import RunOptions, start_guarded_run

    dummy = tmp_path / "mcp_dummy.py"
    dummy.write_text("print('ok')\n", encoding="utf-8")
    store_root = tmp_path / "state"
    result = start_guarded_run(["python", str(dummy)], RunOptions(store_dir=store_root, poll_interval=0.05))
    return SentinelMCPServer(store_dir=store_root), result.run_id


def test_a_blank_outcome_summary_is_not_evidence(tmp_path) -> None:
    """An empty before/after summary must not satisfy the evidence rule.

    A client sending `before_summary=""` means "nothing here". Treating the key's
    mere presence as evidence would let a check pass reproducibility on a blank
    field -- the silent hole this rule exists to close.
    """
    server, run_id = _server_with_a_run(tmp_path)
    message = _error(_call(server, "agentacct_record_machine_check", {
        "source": "codex", "run_id": run_id, "name": "smoke", "result": "passed",
        "evidence_type": "smoke", "section_id": "blank-outcome",
        "before_summary": "", "after_summary": "   ",
    }))
    assert message is not None, "a blank outcome pair must not be accepted as evidence"
    assert "command" in message and "files" in message


def test_a_real_outcome_summary_is_evidence(tmp_path) -> None:
    """The other direction: a repair recorded with real summaries still lands."""
    server, run_id = _server_with_a_run(tmp_path)
    stored = _stored(_call(server, "agentacct_record_machine_check", {
        "source": "codex", "run_id": run_id, "name": "smoke", "result": "passed",
        "evidence_type": "smoke", "section_id": "real-outcome",
        "before_summary": "failed before", "after_summary": "passed after",
    }))
    assert stored["event"]["metadata"]["result"] == "passed"
