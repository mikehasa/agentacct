"""The record's PURPOSE: the goal, the consequence, and what a failure costs.

A reviewer arrives at a record page with four questions — what was this for,
did it work, can I trust it, what do I do now — and before this module the
Python contract could answer only the middle two. Three things were missing and
are covered here:

* **the goal** (`task_goal`): a task-level statement of what the work was FOR,
  recorded once. Without it "completed" is unjudgeable. It cannot be derived:
  ``objectives`` is section titles echoed back, and section titles are STEPS.
* **the consequence**: a summary may state a true outcome and still leave a
  reader with nothing to decide, so the `summary` description asks for the
  consequence first and shows the same facts written both ways.
* **the cost of a failure** (`rest_of_work`): whether the rest of the work is
  still usable. A bounded field, because the page has to group on it; the
  sentence naming the cost stays in the prose slot the record already has.

Plus the display vocabulary the record page prints instead of a tail of named
absences: the collapsed absence budget and the two composed meta lines.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentacct.display_vocabulary import (
    META_SEPARATOR,
    NEXT_STEP_ABSENT,
    NOT_CAPTURED_INLINE_MAX,
    NOT_CAPTURED_NOUNS,
    NOT_CAPTURED_PREFIX,
    RECEIPT_FIELD_LABELS,
    TASK_GOAL_ABSENT,
    check_meta_line,
    checks_heading_line,
    not_captured_line,
)
from agentacct.mcp import (
    REST_OF_WORK_DESCRIPTION,
    TASK_GOAL_DESCRIPTION,
    TOOLS,
    SentinelMCPServer,
)
from agentacct.semantic_rules import (
    REST_OF_WORK_LABELS,
    REST_OF_WORK_STATES,
    check_advisories,
    failure_cost_advisory,
    rest_of_work_label,
    rest_of_work_state,
    section_advisories,
    task_goal_advisory,
)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

GOAL = "Money columns from the bank CSV import without manual cleanup."


def _payload(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response.get("error")
    return json.loads(response["content"][0]["text"])


def _section(server: SentinelMCPServer, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "source": "claude-code",
        "client": "claude-code",
        "client_session_id": "sess-purpose",
        "section_id": "parse-money",
        "section_status": "started",
        "section_title": "Parse money strings",
        "kind": "implementation",
    }
    arguments.update(overrides)
    return _payload(server.call_tool("agentacct_record_section", arguments))


def _check(server: SentinelMCPServer, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "source": "claude-code",
        "name": "parse_amount() rejects unclosed parentheses",
        "result": "failed",
        "exit_code": 1,
        "evidence_type": "test",
        "command": "pytest tests/test_parse.py",
        "section_id": "parse-money",
        "summary": 'parse_amount("($12.34") returns "12.34" instead of raising ValueError.',
    }
    arguments.update(overrides)
    return _payload(server.call_tool("agentacct_record_machine_check", arguments))


def _codes(payload: dict[str, Any]) -> list[str]:
    return [advisory["code"] for advisory in payload.get("advisories", [])]


# --------------------------------------------------------------------------- #
# A1 — the goal field                                                          #
# --------------------------------------------------------------------------- #


def test_record_section_accepts_and_stores_a_task_goal(tmp_path) -> None:
    """The recorder-side setter for `dimensions.task.goal`. It is stored on the
    section record verbatim, so a reducer can hoist it onto the task."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _section(server, task_goal=GOAL)
    assert payload["event"]["metadata"]["task_goal"] == GOAL
    assert _codes(payload) == []


def test_the_goal_is_a_declared_argument_with_a_description_that_teaches(tmp_path) -> None:
    """The schema must carry the rule, not just the field: the last contract
    round's measured lesson is that descriptions teach where refusals block."""

    schema = {tool["name"]: tool["inputSchema"] for tool in TOOLS}["agentacct_record_section"]
    goal = schema["properties"]["task_goal"]
    assert goal["type"] == ["string", "null"]
    assert goal["description"] == TASK_GOAL_DESCRIPTION
    # It asks for the REQUESTER's terms, and says plainly what it is not.
    assert "requester's terms" in TASK_GOAL_DESCRIPTION
    assert "not what you are about to do" in TASK_GOAL_DESCRIPTION
    # A worked contrast, not a rule: one good example and the two ways it goes
    # wrong (a step, and a status).
    assert "Good:" in TASK_GOAL_DESCRIPTION
    assert TASK_GOAL_DESCRIPTION.count("Bad") == 2
    # Recorded ONCE per task, so later sections know to leave it out.
    assert "once" in TASK_GOAL_DESCRIPTION


def test_an_opening_section_without_a_goal_is_advised_and_still_stored(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _section(server)
    assert payload["event"]["event_type"] == "section_started"
    assert _codes(payload) == ["task_without_goal"]
    assert any("task_goal" in warning for warning in payload["warnings"])


def test_a_later_section_inherits_the_goal_and_is_not_nagged(tmp_path) -> None:
    """The goal is recorded once per task. A second section that stays silent
    about it must not be advised, or the advisory becomes the noise it exists
    to remove."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _section(server, task_goal=GOAL)
    later = _section(server, section_id="guard-strip", section_title="Guard the strip path")
    assert _codes(later) == []


def test_a_goal_in_another_session_scope_does_not_silence_the_advisory(tmp_path) -> None:
    """The scan is scoped: one task's goal must never stand in for another's."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _section(server, task_goal=GOAL)
    other = _section(
        server,
        client_session_id="sess-other",
        section_id="other-task",
        section_title="Unrelated work",
    )
    assert _codes(other) == ["task_without_goal"]


def test_a_recorded_goal_is_accepted_as_the_agent_wrote_it(tmp_path) -> None:
    """The advisory is about PRESENCE. What the goal says is taught by the
    field description and shown to the reader, never graded -- not for being
    short, and not for matching the section title."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    for index, goal in enumerate((GOAL, "Parse money strings", "Ship it")):
        payload = _section(
            server,
            section_id=f"goal-{index}",
            section_title="Parse money strings",
            task_goal=goal,
        )
        assert payload["event"]["metadata"]["task_goal"] == goal
        assert _codes(payload) == []


def test_the_missing_goal_advisory_fires_only_on_an_opening_section() -> None:
    """A terminal call is not where the requester's words are still to hand,
    and nagging there would only add a warning to every close."""

    for status in ("checkpoint", "completed", "blocked", "handed_off"):
        assert task_goal_advisory(section_status=status, task_goal=None) is None
    assert task_goal_advisory(section_status="started", task_goal=None) is not None


def test_the_goal_has_a_label_and_a_named_absence() -> None:
    """SS1 renders one recorded sentence, or one named absence in the same
    slot — the first of the two absences exempt from the budget."""

    assert RECEIPT_FIELD_LABELS["goal"] == "Goal"
    assert TASK_GOAL_ABSENT == "No goal was recorded for this task."
    # It is a sentence a reader reads, not a tile qualifier.
    assert TASK_GOAL_ABSENT.endswith(".")


# --------------------------------------------------------------------------- #
# A2 — the summary must ask for the consequence                                #
# --------------------------------------------------------------------------- #

# The real, fully-compliant summary a reviewer called meaningless. Every clause
# is true and it never says what the work is FOR or what the failure COSTS.
WEAK_SUMMARY = (
    "Handing off with parenthesised negatives working and one case still red. "
    '($12.34) and ($1,234.56) parse to the signed value; parse_amount("($12.34") still '
    'returns "12.34" instead of raising ValueError. 6 of 7 parse tests pass, the full '
    "suite is 15 passed 1 failed, and the change is uncommitted in the working tree."
)

STRONG_SUMMARY = (
    "Bank-CSV money strings now parse, except a bare unclosed \"($12.34\", which returns a "
    "positive value instead of raising -- so the importer must not run on unvalidated "
    "input yet. parse_amount() in moneyutil/core.py; 6 of 7 parse tests pass."
)


def test_a_terminal_mechanism_only_summary_is_stored_as_sent_and_not_advised(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _section(
        server,
        task_goal=GOAL,
        section_status="handed_off",
        summary=WEAK_SUMMARY,
        next_step="Guard the strip() path in parse_amount and re-run the parse tests.",
        files=["moneyutil/core.py"],
    )
    # The description teaches the consequence; the write path shows what the
    # agent wrote and never grades its prose.
    assert payload["event"]["metadata"]["summary"] == WEAK_SUMMARY
    assert _codes(payload) == []


def test_the_summary_description_carries_a_worked_weak_vs_strong_contrast() -> None:
    """A2: the description is the lever. It must ask for the consequence FIRST
    and show the same facts written both ways."""

    schema = {tool["name"]: tool["inputSchema"] for tool in TOOLS}["agentacct_record_section"]
    description = schema["properties"]["summary"]["description"]
    assert description.startswith("Open with the CONSEQUENCE")
    assert "what a reader should now believe or do" in description
    assert "then the mechanism" in description
    assert "WEAK" in description and "STRONG" in description
    # The contrast must be the SAME work written both ways, or it teaches
    # nothing: both halves name parse_amount and the same test count.
    weak = description.split("WEAK", 1)[1].split("STRONG", 1)[0]
    strong = description.split("STRONG", 1)[1]
    for half in (weak, strong):
        assert "parse_amount()" in half
        assert "6 of 7 parse tests" in half
    # Only the strong half names what the failure costs.
    assert "must not be pointed at unvalidated input" in strong
    assert "must not be pointed at unvalidated input" not in weak
    # And it says why a count is not a consequence.
    assert "'one case still red' does not tell a reader whether they are blocked" in description


# --------------------------------------------------------------------------- #
# A3 — what a failure costs                                                    #
# --------------------------------------------------------------------------- #


def test_the_cost_of_a_failure_is_a_bounded_field_on_both_recording_tools() -> None:
    """FIELD, not a fifth prose slot: the page has to group and collapse on the
    answer, and a sentence cannot be sorted. Three states, so `unknown` is
    sayable rather than implied by silence."""

    assert REST_OF_WORK_STATES == ("usable", "unusable", "unknown")
    by_name = {tool["name"]: tool["inputSchema"]["properties"] for tool in TOOLS}
    for tool_name in ("agentacct_record_section", "agentacct_record_machine_check"):
        field = by_name[tool_name]["rest_of_work"]
        assert field["enum"] == [*REST_OF_WORK_STATES, None]
        assert field["description"] == REST_OF_WORK_DESCRIPTION
    # The description says what it costs, where the sentence goes, and why a
    # count is not an answer.
    assert "COSTS" in REST_OF_WORK_DESCRIPTION
    assert "'One case still red' does not answer it." in REST_OF_WORK_DESCRIPTION
    assert "in the prose you are already writing" in REST_OF_WORK_DESCRIPTION


def test_every_state_has_a_reviewers_phrase() -> None:
    assert set(REST_OF_WORK_LABELS) == set(REST_OF_WORK_STATES)
    assert rest_of_work_label("unusable") == "this blocks the rest of the work"
    # Never guessed: a record that did not say stays unsaid.
    assert rest_of_work_state(None) is None
    assert rest_of_work_state("mostly") is None
    assert rest_of_work_label("mostly") is None


def test_a_failing_check_that_does_not_say_what_it_costs_is_advised(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _check(server)
    assert payload["event"]["metadata"]["result"] == "failed"
    assert "failure_without_cost" in _codes(payload)


def test_a_failing_check_that_says_what_it_costs_is_not_advised(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _check(server, rest_of_work="usable")
    assert payload["event"]["metadata"]["rest_of_work"] == "usable"
    assert _codes(payload) == []


def test_a_passing_check_is_never_asked_what_it_costs() -> None:
    assert (
        check_advisories(
            name="parse_amount() rejects junk",
            result="passed",
            exit_code=0,
            command="pytest tests/test_parse.py",
        )
        == []
    )


def test_every_declared_cost_state_is_a_complete_answer() -> None:
    """The BIT is the field; the SENTENCE stays in the slot the record already
    has, where the description asks for it and the reader sees it."""

    for state in REST_OF_WORK_STATES:
        assert failure_cost_advisory(reports_a_failure=True, rest_of_work=state) is None


def test_a_blocked_section_is_asked_what_the_stop_costs(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _section(
        server,
        task_goal=GOAL,
        section_status="blocked",
        blocker="parse_amount raises decimal.InvalidOperation on the stripped string.",
        next_step="Guard the strip() path and re-run the parse tests.",
        files=["moneyutil/core.py"],
        summary=STRONG_SUMMARY,
    )
    assert "failure_without_cost" in _codes(payload)
    assert payload["event"]["event_type"] == "section_blocked"


def test_a_completed_section_is_never_asked_what_a_failure_costs() -> None:
    """`completed` is not a failure report, so the question does not arise."""

    codes = [
        advisory["code"]
        for advisory in section_advisories(
            section_title="Parse money strings",
            section_status="completed",
        )
    ]
    assert "failure_without_cost" not in codes


def test_nothing_about_the_cost_is_ever_a_refusal(tmp_path) -> None:
    """Rendered instruction files are written once at onboard and never
    refreshed, so a new refusal would break every already-onboarded agent for a
    field their instructions never mention. It teaches and advises instead."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    for payload in (
        _check(server),
        _section(
            server,
            section_id="stop",
            section_status="blocked",
            blocker="parse_amount raises decimal.InvalidOperation on the stripped string.",
            next_step="Guard the strip() path.",
            files=["moneyutil/core.py"],
            summary=STRONG_SUMMARY,
        ),
    ):
        assert "event" in payload, payload
        assert payload["event"]["event_id"]


def test_an_unknown_cost_state_is_refused_rather_than_stored_raw(tmp_path) -> None:
    """Bounded means bounded: a fourth state would be a second vocabulary."""

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    with pytest.raises(Exception) as caught:
        server.call_tool(
            "agentacct_record_section",
            {
                "source": "claude-code",
                "section_id": "stop",
                "section_status": "started",
                "section_title": "Parse money strings",
                "rest_of_work": "mostly",
            },
        )
    assert "rest_of_work" in str(caught.value)


# --------------------------------------------------------------------------- #
# A4 — the display strings the record page prints                              #
# --------------------------------------------------------------------------- #


def test_the_absence_budget_collapses_to_one_line() -> None:
    line = not_captured_line(["model", "cost", "tool_call_order", "session_identity"])
    assert line == "not captured: model, cost, ordered tool calls, session identity"
    assert line.startswith(f"{NOT_CAPTURED_PREFIX}:")


def test_an_empty_budget_prints_nothing_not_a_positive_claim() -> None:
    """A record knows what it FAILED to capture, not what there was to capture,
    so it must never claim 'everything was captured'."""

    assert not_captured_line([]) is None
    assert not_captured_line(["not_a_known_absence"]) is None


def test_declaration_order_is_render_order_so_two_records_word_it_the_same() -> None:
    keys = list(NOT_CAPTURED_NOUNS)
    forward = not_captured_line(keys[:3])
    backward = not_captured_line(list(reversed(keys[:3])))
    assert forward == backward
    assert forward is not None
    nouns = forward.split(": ", 1)[1].split(", ")
    assert nouns == [NOT_CAPTURED_NOUNS[key] for key in keys[:3]]


def test_the_budget_overflows_to_a_count_rather_than_a_paragraph() -> None:
    keys = list(NOT_CAPTURED_NOUNS)
    assert len(keys) > NOT_CAPTURED_INLINE_MAX
    line = not_captured_line(keys)
    assert line is not None
    overflow = len(keys) - NOT_CAPTURED_INLINE_MAX
    assert line.endswith(f"and {overflow} more")
    inline = line.split(": ", 1)[1].split(", ")[:NOT_CAPTURED_INLINE_MAX]
    assert inline == [NOT_CAPTURED_NOUNS[key] for key in keys[:NOT_CAPTURED_INLINE_MAX]]


def test_a_raw_payload_key_is_never_printed_to_a_reviewer() -> None:
    line = not_captured_line(["model", "weekly_plan_share", "unknown_key"])
    assert line == "not captured: model, weekly plan share"
    assert "weekly_plan_share" not in line
    assert "unknown_key" not in line


def test_the_budget_deduplicates_repeated_keys() -> None:
    assert not_captured_line(["cost", "cost", "cost"]) == "not captured: cost"


def test_a_check_row_gets_one_separated_line_not_four_fragments() -> None:
    """The measured failure: `Passed. Exit 0. test. Agent-reported` — four
    fragments punctuated as four sentences."""

    line = check_meta_line("Passed", 0, "test", "Agent-reported")
    assert line == "Passed · Exit 0 · test · Agent-reported"
    assert "." not in line
    assert line.count(META_SEPARATOR) == 3


def test_a_hoisted_field_is_simply_absent_from_the_row() -> None:
    """When a field is identical on every row it belongs on the heading; the
    row composes nothing and prints nothing for it."""

    assert check_meta_line("Failed", 1) == "Failed · Exit 1"
    assert check_meta_line("Failed", None, None, None) == "Failed"
    assert check_meta_line() == ""


def test_exit_zero_is_a_fact_not_an_absence() -> None:
    """`Exit 0` must survive a falsiness test; only a missing code disappears."""

    assert "Exit 0" in check_meta_line("Passed", 0)
    assert "Exit" not in check_meta_line("Passed", None)
    # A bool is not an exit code.
    assert "Exit" not in check_meta_line("Passed", True)


def test_the_checks_heading_carries_the_tally_and_whatever_was_hoisted() -> None:
    line = checks_heading_line("1/2 passed · 1 failed", "self-checked", "Agent-reported", "test")
    assert line == "1/2 passed · 1 failed · self-checked · Agent-reported · test"
    # Nothing hoisted: the tally stands alone rather than dragging separators.
    assert checks_heading_line("2/2 passed") == "2/2 passed"


def test_the_four_section_headings_are_vocabulary_not_swift_literals() -> None:
    """Today `ReceiptSection(title: "Steps"/"Usage"/"Recording")` are hard-coded
    in Swift. The four questions' headings come from here instead."""

    for key, label in (
        ("goal", "Goal"),
        ("outcome_section", "Outcome"),
        ("evidence_section", "Evidence"),
        ("next_section", "Next"),
    ):
        assert RECEIPT_FIELD_LABELS[key] == label


def test_the_two_exempt_absences_are_named_sentences() -> None:
    """Absence stays NAMED. These two keep their own slot: a record with no
    stated purpose and no stated continuation is one a reviewer should
    distrust."""

    assert TASK_GOAL_ABSENT == "No goal was recorded for this task."
    assert NEXT_STEP_ABSENT == "No next step recorded."
    # Neither is folded into the budget line.
    for sentence in (TASK_GOAL_ABSENT, NEXT_STEP_ABSENT):
        assert NOT_CAPTURED_PREFIX not in sentence
