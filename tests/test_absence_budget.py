"""The absence budget, the deduplicated revision contradiction, the summary
clipped where a sentence ends, and the composed check meta line.

The premise, measured on a real render of task_5f7dbea9: the bottom half of the
record page was eight statements of which six said "we do not know", and the
same revision stamp printed four times for two distinct values while the same
contradiction sentence printed verbatim on two rows. Absence stays a NAMED state
in every test below -- what changes is that it is named ONCE.
"""

from __future__ import annotations

from typing import Any

from agentacct import display_vocabulary as vocab
from agentacct.receipt import (
    CHECK_GROUPING_BY_REVISION,
    CHECK_GROUPING_TIME_ORDER,
    build_receipt,
)


# --- fixtures ---------------------------------------------------------------


def _check(
    result: str,
    *,
    name: str,
    at: float,
    exit_code: int = 0,
    kind: str = "test",
    source_type: str = "agent_reported",
    summary: str | None = None,
    files: list[str] | None = None,
    commit: str | None = None,
    absent_files: list[str] | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    check: dict[str, Any] = {
        "event_id": f"evt_{name}_{int(at)}",
        "result": result,
        "name": name,
        "evidence_type": kind,
        "created_at": at,
        "exit_code": exit_code,
        "source_type": source_type,
        "source": "claude-code",
        "check_identity": f"check:{name}",
        "check_identity_stable": True,
        "files": files or [],
    }
    if summary is not None:
        check["summary"] = summary
    if commit is not None:
        check["git_commit"] = commit
        check["git_branch"] = "main"
        check["git_revision_basis"] = "server_captured_at_record"
        check["git_dirty"] = True
    if absent_files is not None:
        check["git_declared_files_absent"] = absent_files
    if supersedes is not None:
        check["supersedes_check_event_id"] = supersedes
    return check


def _task(
    *,
    task_checks: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    models: list[str] | None = None,
    plan_share: dict[str, Any] | None = None,
    project: str | None = "acme",
    identity_scope_state: str = "explicit",
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "task_x",
        "primary_root": {"client": "claude-code", "client_session_id": "s1"},
        "root_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "session_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "sessions": [
            {
                "client": "claude-code",
                "client_session_id": "s1",
                "project": project,
                "identity_scope_state": identity_scope_state,
                "last_activity_at": 100.0,
                "usage": {},
            }
        ],
        "session_count": 1,
        "supporting_count": 0,
        "child_count": 0,
        "internal_count": 0,
        "last_activity_at": 100.0,
        "work_items": [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        "work_associations": [],
        "usage": usage if usage is not None else {"rows": 0},
        "models": models if models is not None else [],
        "actions": actions
        if actions is not None
        else {
            "tool_category_counts": {},
            "tool_category_total": 0,
            "touched_files": [],
            "touched_file_count": 0,
        },
    }
    if plan_share is not None:
        task["plan_share"] = plan_share
    if task_checks is not None:
        task["current_check_events"] = task_checks
    return task


def _receipt(task: dict[str, Any]) -> dict[str, Any]:
    return build_receipt(task, public_task_id="task_x", title="Add parse_amount()")


#: The Actions shape of task_5f7dbea9: paths were recorded, no tool call was
#: instrumented -- so BOTH ``tool_calls`` and ``tool_call_order`` are absent,
#: which is what makes the subsumption observable.
_PATHS_BUT_NO_CALLS = {
    "tool_category_counts": {},
    "tool_category_total": 0,
    "touched_files": ["moneyutil/core.py", "tests/test_parse.py"],
    "touched_file_count": 2,
}


# The four check rows of task_5f7dbea9, with its measured summaries and stamps:
# two distinct commits, two rows each, and ONE contradiction sentence shared by
# the two rows of the first commit.
_C41 = "c41d44f6d22f94069f52a16513c417f506f3998a"
_25B = "25b39016ce25eef7a8fe1c822b33af3e4178dd18"
_FAILING_SUMMARY = (
    '3 of 4 parse cases fail. The naive lstrip("$") cleanup never removes the symbol '
    'when a sign comes first: parse_amount("-$1,234.56") raises decimal.InvalidOperation '
    'on the string "-$1234.56" instead of returning "-1234.56". Same cause for the '
    "round-trip case, and the junk case raises InvalidOperation where the test expects "
    "ValueError."
)


def _measured_checks() -> list[dict[str, Any]]:
    return [
        _check(
            "failed",
            name="parse_amount() round-trips format_amount output",
            at=100.0,
            exit_code=1,
            summary=_FAILING_SUMMARY,
            files=["tests/test_parse.py", "moneyutil/core.py"],
            commit=_C41,
            absent_files=["tests/test_parse.py"],
        ),
        _check(
            "passed",
            name="parse_amount() handles sign-before-symbol and rejects junk",
            at=200.0,
            exit_code=0,
            summary="All 4 parse cases pass after the regex rewrite.",
            files=["moneyutil/core.py", "tests/test_parse.py"],
            commit=_C41,
            absent_files=["tests/test_parse.py"],
        ),
        _check(
            "failed",
            name="parse_amount() reads ($12.34) as -12.34",
            at=300.0,
            exit_code=1,
            summary="3 new accounting-notation cases fail against the current regex.",
            files=["tests/test_parse.py"],
            commit=_25B,
        ),
        _check(
            "failed",
            name="Half-open ($12.34 must not parse",
            at=400.0,
            exit_code=1,
            summary="Down to one failure from three.",
            files=["moneyutil/core.py", "tests/test_parse.py"],
            commit=_25B,
        ),
    ]


# --- P1: one absence line ---------------------------------------------------


def _measured_task() -> dict[str, Any]:
    """The absence shape of task_5f7dbea9: no model, no usage, paths but no
    instrumented tool calls, and a session identity that came from the agent's own
    report."""

    return _task(task_checks=_measured_checks(), actions=dict(_PATHS_BUT_NO_CALLS))


def _measured_rows() -> list[dict[str, Any]]:
    return _receipt(_measured_task())["dimensions"]["evidence"]["checks"]


def test_the_record_states_every_absence_on_one_line_in_declaration_order() -> None:
    """Six "we do not know" statements become one line of nouns. The ORDER is the
    noun table's, never the reducer's append order, so two records can never word
    the same set of absences differently."""

    receipt = _receipt(_measured_task())
    budget = receipt["dimensions"]["gaps"]["not_captured"]
    assert budget["line"] == "not captured: model, cost, tool calls, session identity"
    assert budget["keys"] == ["model", "cost", "tool_calls", "session_identity"]


def test_every_absence_keeps_its_full_sentence_behind_the_line() -> None:
    """The line is a presentation change, not a retreat from honesty: each noun's
    own sentence survives in the disclosure, worded exactly as the gap list words
    it, so the two can never disagree."""

    receipt = _receipt(_measured_task())
    gaps = receipt["dimensions"]["gaps"]
    budget = gaps["not_captured"]
    sentences = {item["reason"] for item in gaps["items"]}
    for row in budget["detail"]:
        assert row["text"], f"{row['key']} lost its sentence"
        if row["key"] != "weekly_plan_share":
            assert row["text"] in sentences
    # The disclosure carries MORE than the line: the subsumed nouns are spared a
    # second wording on the line, never deleted.
    assert budget["detail_count"] >= len(budget["keys"])
    assert {row["key"] for row in budget["detail"]} >= set(budget["keys"])


def test_the_disclosure_reads_in_the_same_order_as_the_line_it_opens() -> None:
    receipt = _receipt(_measured_task())
    budget = receipt["dimensions"]["gaps"]["not_captured"]
    order = list(vocab.NOT_CAPTURED_NOUNS)
    positions = [order.index(row["key"]) for row in budget["detail"]]
    assert positions == sorted(positions)


def test_an_empty_budget_prints_nothing_rather_than_claiming_completeness() -> None:
    """A record knows what it FAILED to capture, never that it captured
    everything. With nothing absent the line is None and the surface prints no
    line at all -- never "all captured"."""

    task = _task(
        usage={
            "rows": 2,
            "estimated_cost_usd": 0.5,
            "cost_complete": True,
            "cost_basis": "pricing_table",
            "cost_confidence": "estimated_from_tokens",
            "total_tokens": 1000,
        },
        models=["claude-opus"],
        actions={
            "tool_category_counts": {"edit": 3},
            "tool_category_total": 3,
            "touched_files": [],
            "touched_file_count": 0,
            "capture_bases": ["client_hook"],
        },
        plan_share={"pct": 4.0, "calibration_state": "calibrated", "client": "claude-code"},
    )
    task["sessions"][0]["usage"] = {"rows": 2}
    budget = _receipt(task)["dimensions"]["gaps"]["not_captured"]
    assert budget["keys"] == []
    assert budget["line"] is None
    assert vocab.not_captured_line([]) is None


def test_no_tool_call_captured_is_not_also_stated_as_no_ORDER_of_tool_calls() -> None:
    """A record that captured no tool call at all cannot separately be missing
    their order -- that is one absence worded twice, which is exactly the
    duplication the line exists to remove. Both stay in the disclosure."""

    receipt = _receipt(_measured_task())
    budget = receipt["dimensions"]["gaps"]["not_captured"]
    assert "tool_calls" in budget["keys"]
    assert "tool_call_order" not in budget["keys"]
    assert "ordered tool calls" not in (budget["line"] or "")
    # Named, not deleted.
    assert "tool_call_order" in {row["key"] for row in budget["detail"]}


def test_an_unpriced_record_is_not_also_missing_its_share_of_a_price() -> None:
    receipt = _receipt(
        _task(
            task_checks=_measured_checks(),
            plan_share={
                "pct": None,
                "calibration_state": "calibrated",
                "client": "claude-code",
                "sentence_text": "no priced usage in this session",
            },
        )
    )
    budget = receipt["dimensions"]["gaps"]["not_captured"]
    assert "cost" in budget["keys"] and "weekly_plan_share" not in budget["keys"]
    detail = {row["key"]: row["text"] for row in budget["detail"]}
    assert detail["weekly_plan_share"] == "no priced usage in this session"


def test_the_ordered_tool_calls_noun_is_used_when_calls_WERE_captured() -> None:
    """The subsumption is a claim about what the words mean, not a way to drop a
    noun: with tool calls captured, their missing ORDER is its own absence and
    the line says so."""

    receipt = _receipt(
        _task(
            task_checks=_measured_checks(),
            actions={
                "tool_category_counts": {"edit": 3},
                "tool_category_total": 3,
                "touched_files": ["moneyutil/core.py"],
                "touched_file_count": 1,
                "capture_bases": ["client_hook"],
            },
        )
    )
    budget = receipt["dimensions"]["gaps"]["not_captured"]
    assert "tool_call_order" in budget["keys"] and "tool_calls" not in budget["keys"]
    assert "ordered tool calls" in budget["line"]


def test_an_unscoped_task_names_the_project_as_a_noun_not_a_sentence() -> None:
    receipt = _receipt(
        _task(
            task_checks=_measured_checks(),
            project=None,
            identity_scope_state="unscoped",
            models=["claude-opus"],
        )
    )
    budget = receipt["dimensions"]["gaps"]["not_captured"]
    assert "project" in budget["keys"]
    assert budget["line"] == "not captured: cost, tool calls, session identity, project"


def test_a_budget_past_the_inline_cap_counts_the_rest_rather_than_dropping_it() -> None:
    """Five nouns is a paragraph at the record page's measure, so the line names
    four and counts the rest. The disclosure still carries every one."""

    receipt = _receipt(
        _task(
            task_checks=_measured_checks(),
            actions=dict(_PATHS_BUT_NO_CALLS),
            project=None,
            identity_scope_state="unscoped",
        )
    )
    budget = receipt["dimensions"]["gaps"]["not_captured"]
    assert budget["line"] == (
        "not captured: model, cost, tool calls, session identity, and 1 more"
    )
    assert "project" in budget["keys"]
    assert "project" in {row["key"] for row in budget["detail"]}


def test_a_gap_no_noun_could_carry_stays_a_sentence_outside_the_budget() -> None:
    """``No description of the change was recorded`` names something the record
    CONTRADICTS, not a measurement it failed to take. A noun cannot carry it, so
    it stays a full sentence in the gap list and never enters the line."""

    receipt = _receipt(_task())
    gaps = receipt["dimensions"]["gaps"]
    unbudgeted = [
        item["reason"]
        for item in gaps["items"]
        if item["absence_key"] is None
    ]
    assert any("description of the change" in reason for reason in unbudgeted)
    assert "change" not in (gaps["not_captured"]["line"] or "")


def test_collapse_drops_a_key_the_noun_table_does_not_know() -> None:
    """A raw payload key printed to a reviewer would be a second vocabulary."""

    assert vocab.collapse_not_captured_keys(["model", "no_such_key", ""]) == ["model"]
    assert vocab.collapse_not_captured_keys([]) == []


# --- P2: the contradiction, once ------------------------------------------


def test_one_contradiction_shared_by_two_rows_becomes_one_banner() -> None:
    """The measured duplication: the SAME sentence printed verbatim on two rows
    because it was computed per check. It is now stated once, at the group, and
    the rows are left empty -- one banner, not N copies."""

    evidence = _receipt(_measured_task())["dimensions"]["evidence"]
    group = evidence["revision_groups"][0]
    rows = [
        row
        for row in evidence["checks"]
        if row["event_id"] in group["event_ids"]
    ]
    assert group["row_count"] == 2
    assert group["contradiction_text"] == (
        "The stamped revision c41d44f does not contain tests/test_parse.py, "
        "so it is not the revision this check ran against."
    )
    assert [row["revision_contradiction_text"] for row in rows] == [None, None]
    # The rows still carry the PROOF; only the sentence moved.
    assert all(row["revision_absent_files"] == ["tests/test_parse.py"] for row in rows)


def test_a_stamp_printed_four_times_for_two_values_becomes_two_group_headers() -> None:
    evidence = _receipt(_measured_task())["dimensions"]["evidence"]
    groups = evidence["revision_groups"]
    assert evidence["revision_grouping_mode"] == CHECK_GROUPING_BY_REVISION
    assert [group["label"] for group in groups] == [
        "HEAD when recorded: c41d44f · main · uncommitted changes",
        "HEAD when recorded: 25b3901 · main · uncommitted changes",
    ]
    assert [group["row_count"] for group in groups] == [2, 2]
    # Walking the groups yields every row exactly once, in strict time order --
    # so the grouping settles the render order too and no surface has to sort.
    walked = [id_ for group in groups for id_ in group["event_ids"]]
    by_id = {row["event_id"]: row for row in evidence["checks"]}
    assert sorted(walked) == sorted(by_id)
    assert [by_id[id_]["at"] for id_ in walked] == [100.0, 200.0, 300.0, 400.0]
    # The row keeps its label for a surface that renders no header, and is told
    # the header already prints it.
    assert all(row["revision_label_hoisted"] for row in evidence["checks"])
    assert [by_id[id_]["revision_group_index"] for id_ in walked] == [0, 0, 1, 1]


def test_two_different_contradictions_are_never_merged_into_one_sentence() -> None:
    """A merged sentence covering two different contradictions would be a second
    vocabulary. When the rows disagree there is NO banner and each keeps its
    own."""

    checks = [
        _check(
            "failed",
            name="a",
            at=100.0,
            exit_code=1,
            commit=_C41,
            files=["tests/a.py"],
            absent_files=["tests/a.py"],
        ),
        _check(
            "failed",
            name="b",
            at=200.0,
            exit_code=1,
            commit=_C41,
            files=["tests/b.py"],
            absent_files=["tests/b.py"],
        ),
    ]
    evidence = _receipt(_task(task_checks=checks))["dimensions"]["evidence"]
    assert evidence["revision_groups"][0]["contradiction_text"] is None
    texts = [row["revision_contradiction_text"] for row in evidence["checks"]]
    assert all(texts) and texts[0] != texts[1]
    assert "tests/a.py" in texts[0] and "tests/b.py" in texts[1]


def test_a_lone_row_keeps_its_own_contradiction_rather_than_gaining_a_banner() -> None:
    """Hoisting one row's sentence over one row buys no ink and costs the row the
    sentence. The dedup is earned by a DUPLICATE."""

    checks = [
        _check(
            "failed",
            name="a",
            at=100.0,
            exit_code=1,
            commit=_C41,
            files=["tests/a.py"],
            absent_files=["tests/a.py"],
        )
    ]
    evidence = _receipt(_task(task_checks=checks))["dimensions"]["evidence"]
    assert evidence["revision_groups"][0]["contradiction_text"] is None
    assert "tests/a.py" in evidence["checks"][0]["revision_contradiction_text"]


def test_grouping_falls_back_to_time_order_rather_than_split_a_recovery() -> None:
    """``WorkRecordChecks``' own contract is that a fail -> pass recovery reads as
    two ADJACENT rows. A grouping that files the failure under one commit and its
    fix under another destroys exactly the story the rows exist to tell, so the
    reducer gives up the grouping instead -- and says so on the payload, because
    a surface guessing this would be a second grouping vocabulary."""

    failure = _check("failed", name="pytest", at=100.0, exit_code=1, commit=_C41)
    recovery = _check(
        "passed",
        name="pytest",
        at=200.0,
        exit_code=0,
        commit=_25B,
        supersedes=failure["event_id"],
    )
    evidence = _receipt(_task(task_checks=[failure, recovery]))["dimensions"]["evidence"]
    assert evidence["revision_grouping_mode"] == CHECK_GROUPING_TIME_ORDER
    groups = evidence["revision_groups"]
    # Two single-row runs in strict time order -- still two label prints, and the
    # failure is still immediately followed by its fix.
    assert [group["row_count"] for group in groups] == [1, 1]
    assert [id_ for group in groups for id_ in group["event_ids"]] == [
        failure["event_id"],
        recovery["event_id"],
    ]


def test_grouping_by_revision_survives_a_recovery_inside_one_commit() -> None:
    """The fallback is not the common case: a fail -> pass pair stamped with one
    commit groups normally, and the label prints once for both rows."""

    failure = _check("failed", name="pytest", at=100.0, exit_code=1, commit=_C41)
    recovery = _check(
        "passed", name="pytest", at=200.0, exit_code=0, commit=_C41, supersedes=failure["event_id"]
    )
    evidence = _receipt(_task(task_checks=[failure, recovery]))["dimensions"]["evidence"]
    assert evidence["revision_grouping_mode"] == CHECK_GROUPING_BY_REVISION
    assert len(evidence["revision_groups"]) == 1
    assert evidence["revision_groups"][0]["row_count"] == 2


def test_unstamped_rows_share_one_group_because_absence_is_one_state() -> None:
    checks = [
        _check("passed", name="a", at=100.0),
        _check("passed", name="b", at=200.0),
    ]
    evidence = _receipt(_task(task_checks=checks))["dimensions"]["evidence"]
    groups = evidence["revision_groups"]
    assert len(groups) == 1
    assert groups[0]["label"] == vocab.REVISION_NOT_CAPTURED
    assert groups[0]["revision"] is None


# --- P3: clip the summary where a sentence ends ---------------------------


def test_the_first_sentence_is_kept_whole_even_past_the_budget() -> None:
    """The measured loss: a one-line clamp cut this summary at ``raises
    decimal.InvalidOperation on the st…``. The budget decides how many sentences
    ride along, never where one is cut."""

    long_sentence = _FAILING_SUMMARY.split(". ", 1)[1]
    preview, elided = vocab.check_summary_preview(long_sentence, budget=80)
    assert preview == vocab.split_sentences(long_sentence)[0]
    assert len(preview) > 80
    assert elided is True


def test_further_whole_sentences_ride_along_only_while_they_fit_the_budget() -> None:
    sentences = vocab.split_sentences(_FAILING_SUMMARY)
    assert vocab.check_summary_preview(_FAILING_SUMMARY) == (sentences[0], True)
    roomy = len(" ".join(sentences[:2]))
    assert vocab.check_summary_preview(_FAILING_SUMMARY, budget=roomy) == (" ".join(sentences[:2]), True)


def test_the_preview_never_ends_mid_sentence_and_carries_no_ellipsis() -> None:
    """A run of WHOLE sentences: a full stop already terminates it, so an
    ellipsis would be a second punctuation vocabulary. ``elided`` is what tells a
    surface there is a rest to offer."""

    preview, elided = vocab.check_summary_preview(_FAILING_SUMMARY)
    assert preview.endswith(".")
    assert "…" not in preview and "..." not in preview
    assert elided is True
    assert preview in _FAILING_SUMMARY


def test_a_summary_that_fits_is_handed_over_whole() -> None:
    whole = (
        "All 4 parse cases pass after the regex rewrite: -$1,234.56 now parses to "
        "-1234.56, and twelve dollars raises ValueError."
    )
    preview, elided = vocab.check_summary_preview(whole)
    assert preview == whole and elided is False


def test_a_dotted_identifier_and_a_decimal_are_not_sentence_ends() -> None:
    """``decimal.InvalidOperation`` and ``($1,234.56)`` would each split a
    sentence under a naive full-stop rule, and the preview would then end
    mid-clause after all."""

    assert vocab.split_sentences("parse_amount($1,234.56) raises decimal.InvalidOperation.") == [
        "parse_amount($1,234.56) raises decimal.InvalidOperation."
    ]
    assert vocab.split_sentences('It returns "-1234.56". Same cause elsewhere.') == [
        'It returns "-1234.56".',
        "Same cause elsewhere.",
    ]


def test_the_row_carries_the_preview_beside_the_verbatim_summary() -> None:
    """The preview never replaces the recorded words: a surface offers the rest
    rather than losing it."""

    row = next(row for row in _measured_rows() if row["summary"] == _FAILING_SUMMARY)
    assert row["summary_elided"] is True
    assert row["summary_preview"] and row["summary_preview"] != row["summary"]
    assert row["summary"].startswith(row["summary_preview"])


def test_an_absent_summary_leaves_the_preview_absent_rather_than_empty() -> None:
    assert vocab.check_summary_preview(None) == (None, False)
    assert vocab.check_summary_preview("   ") == (None, False)
    row = _receipt(_task(task_checks=[_check("passed", name="a", at=100.0)]))["dimensions"][
        "evidence"
    ]["checks"][0]
    assert row["summary_preview"] is None and row["summary_elided"] is False


# --- P4: one line, one separator ------------------------------------------


def test_four_facts_punctuated_as_four_sentences_become_one_line() -> None:
    """``Passed. Exit 0. test. Agent-reported`` is four fragments given four full
    stops. One line, one stated separator, composed in the reducer."""

    line = vocab.check_meta_line("Passed", 0, "test", "Agent-reported")
    assert line == "Passed · Exit 0 · test · Agent-reported"
    assert ". " not in line
    # And the reducer is where it is composed: the row ships the line, never the
    # four words for a surface to punctuate.
    row = _receipt(_task(task_checks=[_check("passed", name="a", at=100.0)]))["dimensions"][
        "evidence"
    ]["checks"][0]
    assert ". " not in row["meta_line"]
    assert row["meta_line"].startswith("Passed · Exit 0")


def test_a_fact_identical_on_every_row_is_hoisted_off_the_rows() -> None:
    """On task_5f7dbea9 ``test`` and ``Agent-reported`` are the same on all four
    rows -- six prints of two facts. They move to the heading and the rows keep
    only what distinguishes them."""

    evidence = _receipt(_measured_task())["dimensions"]["evidence"]
    assert [row["meta_line"] for row in evidence["checks"]] == [
        "Failed · Exit 1",
        "Passed · Exit 0",
        "Failed · Exit 1",
        "Failed · Exit 1",
    ]
    assert evidence["hoisted_evidence_type"] == "test"
    assert evidence["hoisted_source_label"] == "Agent-reported"
    assert evidence["heading_line"].endswith("Agent-reported · test")


def test_the_heading_states_the_tier_once_beside_the_tally() -> None:
    """The record's ONE print of the evidence tier. It was stated three times in
    three registers -- a prose sentence, a bare word and a pip."""

    task = _measured_task()
    task["work_items"] = [
        {
            "work_id": "w",
            "latest_status": "completed",
            "updated_at": 100.0,
            "current_check_events": [_check("passed", name="linked", at=150.0)],
        }
    ]
    receipt = _receipt(task)
    evidence = receipt["dimensions"]["evidence"]
    heading = evidence["heading_line"]
    tier = vocab.TIER_LABELS[receipt["axes"]["evidence_strength"]["strongest_tier"]]
    assert heading.startswith(evidence["check_tally_text"])
    assert heading.count(tier) == 1


def test_a_record_that_cannot_name_a_tier_states_none_rather_than_inventing_one() -> None:
    """Every completed step unchecked means there IS no strongest tier. The
    heading omits it; it never falls back to the weakest word."""

    receipt = _receipt(_measured_task())
    assert receipt["axes"]["evidence_strength"]["strongest_tier"] is None
    heading = receipt["dimensions"]["evidence"]["heading_line"]
    assert not any(label in heading for label in vocab.TIER_LABELS.values())


def test_a_fact_that_differs_between_rows_stays_on_the_rows() -> None:
    """Hoisting is earned by being uniform. A record whose checks came from two
    sources states the source per row, because there the source is what tells the
    rows apart."""

    checks = [
        _check("passed", name="a", at=100.0, source_type="agent_reported"),
        _check("passed", name="b", at=200.0, source_type="client_hook"),
    ]
    evidence = _receipt(_task(task_checks=checks))["dimensions"]["evidence"]
    assert evidence["hoisted_source_label"] is None
    assert all("·" in row["meta_line"] for row in evidence["checks"])
    assert {row["meta_line"].rsplit(" · ", 1)[-1] for row in evidence["checks"]} == {
        "Agent-reported",
        "Hook-captured",
    }


def test_exit_zero_survives_a_falsiness_test_and_a_bool_is_not_an_exit_code() -> None:
    assert vocab.check_meta_line("Passed", 0) == "Passed · Exit 0"
    assert vocab.check_meta_line("Passed", True) == "Passed"
    assert vocab.check_meta_line("Passed", None) == "Passed"
    assert vocab.check_meta_line("Passed", "not a number") == "Passed"


def test_the_result_and_exit_code_are_never_hoisted_even_when_uniform() -> None:
    """They are what distinguishes one row from another. A record whose rows all
    passed still says so per row, or the rows stop being rows."""

    checks = [
        _check("passed", name="a", at=100.0),
        _check("passed", name="b", at=200.0),
    ]
    evidence = _receipt(_task(task_checks=checks))["dimensions"]["evidence"]
    assert all(row["meta_line"].startswith("Passed · Exit 0") for row in evidence["checks"])


def test_a_record_with_no_checks_states_no_heading_rather_than_an_empty_one() -> None:
    evidence = _receipt(_task())["dimensions"]["evidence"]
    assert evidence["checks"] == []
    assert evidence["revision_groups"] == []
    assert evidence["hoisted_evidence_type"] is None
    # The tally still leads the heading; nothing was hoisted onto it.
    assert "·" not in (evidence["heading_line"] or "")
