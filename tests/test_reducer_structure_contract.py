"""What the reducers must SHOW, not just store.

Four defects this module pins, each of which let a true record project into a
misleading payload:

* **Checkpoints were collected and destroyed.** The recording contract tells
  every agent to send ``section_status=checkpoint`` updates "rather than one
  giant section". The ledger kept one summary per section and the last write
  won, so the most informative sentence in a Task could be in the store and in
  neither ``/v1/receipt`` nor ``/v1/task-timeline``.
* **Salience was constant.** ``important`` was ``bool(blocker)`` for work (5
  blocked sections in 1,309 across the installed ledger) and an unconditional
  ``True`` for every check, so a 48-record canvas was 48 equally loud rows.
* **Gaps were ordered by which reducer appended first**, not by what they
  prevent.
* **``hidden_in_subagents`` counts steps a subagent RECORDED**, so a subagent
  that recorded nothing pushes it to 0 — the reading a reviewer takes as
  "nothing is hidden" is produced by the case where everything is.

Every expectation is read from the reducer or from ``display_vocabulary`` at
assert time; a test that spells the words itself is a second copy of them.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from agentacct import display_vocabulary as vocabulary
from agentacct.receipt import build_receipt, unrecorded_subagent_sessions
from agentacct.task_timeline import BEAT_KIND, build_timeline_events
from agentacct.work_ledger import build_work_items

# The sentence from task_3c028ac0's checkpoint: the most informative prose in
# that Task's whole record, and (before this change) absent from both payloads.
CHECKPOINT_PROSE = (
    "subtract() landed in fc3b723 with a passing test. Started format_amount(): positive amounts "
    "with grouping work; negative sign placement is not implemented."
)
CLOSING_PROSE = "Both helpers landed; negative sign placement still owes a test."


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _snapshot(
    status: str,
    *,
    section: str = "money-helpers",
    at: float,
    summary: str | None = None,
    session: str = "root",
    files: list[str] | None = None,
    blocker: str | None = None,
    title: str = "Add subtract() and format_amount()",
) -> dict[str, Any]:
    """One stored section snapshot, in the shape ``build_work_items`` reduces."""

    return {
        "event_id": f"evt_{section}_{status}_{int(at)}",
        "created_at": at,
        "work_id": f"claude-code::{session}::{section}",
        "section_id": section,
        "client": "claude-code",
        "source": "claude-code",
        "client_session_id": session,
        "status": status,
        "title": title,
        "kind": "implementation",
        "summary": summary,
        "files": files or [],
        "blocker": blocker,
        "next_step": None,
    }


def _items_from(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return build_work_items(deepcopy(snapshots), [], [])


def _task(items: list[dict[str, Any]], *, sessions: list[dict[str, Any]] | None = None,
          checks: list[dict[str, Any]] | None = None, actions: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "task_id": "task_beats",
        "primary_root": {"client": "claude-code", "client_session_id": "root"},
        "root_keys": [{"client": "claude-code", "client_session_id": "root"}],
        "sessions": sessions
        if sessions is not None
        else [{"client": "claude-code", "client_session_id": "root", "session_kind": "root", "usage": {}}],
        "session_count": len(sessions) if sessions is not None else 1,
        "work_items": items,
        "work_associations": [],
        "current_check_events": checks or [],
        "task_evidence_events": [deepcopy(check) for check in (checks or [])],
        "usage": {"rows": 1, "total_tokens": 1000, "estimated_cost_usd": 0.5, "cost_complete": True},
        "models": ["claude-opus"],
        "actions": actions
        if actions is not None
        else {"tool_category_counts": {}, "tool_category_total": 0, "touched_files": [], "touched_file_count": 0},
    }


def _receipt(task: dict[str, Any]) -> dict[str, Any]:
    return build_receipt(task, public_task_id="task_beats", title="Add subtract() money helper")


def _one_section_with_a_checkpoint() -> list[dict[str, Any]]:
    return [
        _snapshot("started", at=10.0),
        _snapshot("checkpoint", at=20.0, summary=CHECKPOINT_PROSE),
        _snapshot("completed", at=30.0, summary=CLOSING_PROSE, files=["money.py"]),
    ]


# --------------------------------------------------------------------------- #
# E1 — checkpoints are collected and destroyed                                 #
# --------------------------------------------------------------------------- #


def test_the_checkpoint_prose_survives_the_reducer_that_used_to_overwrite_it() -> None:
    """``item["summary"] = event.get("summary") or item["summary"]`` is still
    last-write-wins — the headline must stay the terminal summary — but the
    prose it overwrites now has its own home."""

    item = _items_from(_one_section_with_a_checkpoint())[0]
    assert item["summary"] == CLOSING_PROSE
    assert [note["summary"] for note in item["progress_notes"]] == [CHECKPOINT_PROSE]
    assert item["progress_notes"][0]["status"] == "checkpoint"
    assert item["progress_notes"][0]["at"] == 20.0


def test_the_checkpoint_prose_is_reachable_in_the_timeline_under_its_section() -> None:
    task = _task(_items_from(_one_section_with_a_checkpoint()))
    events = build_timeline_events(task)
    section = next(event for event in events if event["kind"] == "work")
    beats = [event for event in events if event["kind"] == BEAT_KIND]
    assert [beat["summary"] for beat in beats] == [CHECKPOINT_PROSE]
    assert beats[0]["section_record_id"] == section["id"]
    assert beats[0]["section_record_ids"] == [section["id"]]
    assert beats[0]["section_title"] == section["title"]
    # A beat sits in its section's lane, not in a lane of its own.
    assert beats[0]["lane"] == section["lane"] and beats[0]["lane_label"] == section["lane_label"]
    # And it is ordered where it happened, between the section and its close.
    assert section["occurred_at"] <= beats[0]["occurred_at"] <= section["updated_at"]


def test_the_checkpoint_prose_is_reachable_in_the_receipt() -> None:
    """The defect was stated as a grep: the sentence appeared in neither
    payload. This is that grep, run against the reducer output."""

    receipt = _receipt(_task(_items_from(_one_section_with_a_checkpoint())))
    rendered = repr(receipt)
    assert CHECKPOINT_PROSE in rendered
    assert receipt["timeline"]["beat_count"] == 1
    assert receipt["timeline"]["beat_definition"] == vocabulary.TIMELINE_BEAT_DEFINITION


def test_beats_are_ordered_and_one_beat_per_distinct_note() -> None:
    later = "format_amount() now places the sign; the grouping test still fails on 1,000.50."
    snapshots = [
        _snapshot("started", at=10.0),
        _snapshot("checkpoint", at=20.0, summary=CHECKPOINT_PROSE),
        # A snapshot that re-sends the same prose narrated once, not twice.
        _snapshot("checkpoint", at=25.0, summary=CHECKPOINT_PROSE),
        _snapshot("checkpoint", at=28.0, summary=later),
        _snapshot("completed", at=30.0, summary=CLOSING_PROSE, files=["money.py"]),
    ]
    beats = [event for event in build_timeline_events(_task(_items_from(snapshots))) if event["kind"] == BEAT_KIND]
    assert [beat["summary"] for beat in beats] == [CHECKPOINT_PROSE, later]
    assert [beat["occurred_at"] for beat in beats] == [20.0, 28.0]


def test_a_beat_never_restates_the_sections_own_headline() -> None:
    """A section that checkpointed with the sentence it later closed on said it
    once; a beat repeating it would be a duplicate row beside the summary."""

    snapshots = [
        _snapshot("checkpoint", at=20.0, summary=CLOSING_PROSE),
        _snapshot("completed", at=30.0, summary=CLOSING_PROSE, files=["money.py"]),
    ]
    item = _items_from(snapshots)[0]
    assert item["summary"] == CLOSING_PROSE
    assert item["progress_notes"] == []
    assert not [event for event in build_timeline_events(_task([item])) if event["kind"] == BEAT_KIND]


def test_beats_do_not_inflate_step_counts_coverage_denominators_or_the_check_tally() -> None:
    """The same Task with and without its checkpoint snapshot must agree about
    every number a reader could mistake a beat for."""

    check = {
        "event_id": "check-subtract",
        "result": "passed",
        "name": "subtract() rounds half-up",
        "evidence_type": "test",
        "created_at": 29.0,
        "exit_code": 0,
        "source_type": "client_hook",
        "source": "claude-code",
        "section_id": "money-helpers",
        "check_identity": "check:subtract",
        "check_identity_stable": True,
    }
    without = _receipt(_task(_items_from([
        _snapshot("started", at=10.0),
        _snapshot("completed", at=30.0, summary=CLOSING_PROSE, files=["money.py"]),
    ]), checks=[check]))
    with_beat = _receipt(_task(_items_from(_one_section_with_a_checkpoint()), checks=[check]))

    assert with_beat["timeline"]["beat_count"] == 1
    assert without["timeline"]["beat_count"] == 0
    assert with_beat["raw_evidence"]["work_item_count"] == without["raw_evidence"]["work_item_count"] == 1
    for field in (
        "checkable_total",
        "checked_total",
        "by_tier",
        "total_steps",
        "checks_total",
        "checks_passed",
        "checks_failed",
        "checks_not_run",
        "verified_step_count",
        "total_step_count",
        "agent_reported_step_count",
    ):
        assert with_beat["axes"]["evidence_strength"][field] == without["axes"]["evidence_strength"][field], field
    assert with_beat["axes"]["evidence_strength"]["coverage_hero"] == without["axes"]["evidence_strength"]["coverage_hero"]
    assert with_beat["axes"]["evidence_strength"]["check_tally_text"] == without["axes"]["evidence_strength"]["check_tally_text"]


def test_a_section_that_never_checkpointed_gains_nothing() -> None:
    """Forward-only: a stored row written before beats existed projects exactly
    as it did, with an empty list rather than a missing key."""

    item = _items_from([_snapshot("completed", at=30.0, summary=CLOSING_PROSE, files=["money.py"])])[0]
    assert item["progress_notes"] == []
    assert not [event for event in build_timeline_events(_task([item])) if event["kind"] == BEAT_KIND]


def test_a_beat_carries_the_reducers_words_and_never_claims_to_be_a_step() -> None:
    beat = next(
        event
        for event in build_timeline_events(_task(_items_from(_one_section_with_a_checkpoint())))
        if event["kind"] == BEAT_KIND
    )
    assert beat["kind"] != "work"
    assert beat["status_label"] == vocabulary.step_status_label("checkpoint")
    assert beat["time_note"] == vocabulary.TIMELINE_BEAT_TIME_NOTE
    assert beat["title"] and beat["title"] != vocabulary.TIMELINE_BEAT_TITLE  # the prose reduced to a label
    # Narration is never salient on its own: its section carries the mark.
    assert beat["important"] is False and beat["salience"] is None


# --------------------------------------------------------------------------- #
# E2 — salience is constant                                                    #
# --------------------------------------------------------------------------- #


def _failed_check(section: str, at: float) -> dict[str, Any]:
    return {
        "event_id": f"check_{section}",
        "result": "failed",
        "name": "grouping test",
        "evidence_type": "test",
        "created_at": at,
        "exit_code": 1,
        "source_type": "client_hook",
        "source": "claude-code",
        "section_id": section,
        "summary": "AssertionError: expected 1,000.50, got 1000.5",
        "check_identity": f"check:{section}",
        "check_identity_stable": True,
    }


def _mixed_sections() -> dict[str, Any]:
    """Four sections that a reviewer would rank differently."""

    quiet = {
        "work_id": "quiet", "section_id": "quiet", "title": "Update the changelog",
        "client": "claude-code", "client_session_id": "root", "latest_status": "completed",
        "kind": "docs", "started_at": 10.0, "updated_at": 11.0, "files": ["CHANGELOG.md"],
        "summary": "Added the release note.", "progress_notes": [],
    }
    failing = {
        "work_id": "failing", "section_id": "failing", "title": "Implement format_amount()",
        "client": "claude-code", "client_session_id": "root", "latest_status": "completed",
        "kind": "implementation", "started_at": 12.0, "updated_at": 13.0, "files": ["money.py"],
        "summary": "Sign placement done; grouping still wrong.", "progress_notes": [],
        "evidence_events": [_failed_check("failing", 13.0)],
    }
    stranded = {
        "work_id": "stranded", "section_id": "stranded", "title": "Wire the CLI flag",
        "client": "claude-code", "client_session_id": "root", "latest_status": "checkpoint",
        "kind": "implementation", "started_at": 14.0, "updated_at": 15.0, "files": ["cli.py"],
        "summary": "Parsing works; the flag is not threaded through yet.", "progress_notes": [],
    }
    # Checked, so its only distinguishing fact is its size.
    broad_check = {
        "event_id": "check_broad", "result": "passed", "name": "import smoke test",
        "evidence_type": "test", "created_at": 17.0, "exit_code": 0, "source_type": "client_hook",
        "source": "claude-code", "section_id": "broad", "check_identity": "check:broad",
        "check_identity_stable": True,
    }
    broad = {
        "work_id": "broad", "section_id": "broad", "title": "Rename the money module",
        "client": "claude-code", "client_session_id": "root", "latest_status": "completed",
        "kind": "refactor", "started_at": 16.0, "updated_at": 17.0,
        "files": ["a.py", "b.py", "c.py", "d.py"],
        "summary": "Renamed across the package.", "progress_notes": [],
        "evidence_events": [deepcopy(broad_check)],
    }
    return _task([quiet, failing, stranded, broad], checks=[_failed_check("failing", 13.0), broad_check])


def test_salience_varies_between_the_sections_of_one_task() -> None:
    """The defect: with ``important = bool(blocker)`` every one of these four
    rows was equally quiet. A mark that is always off says as little as one
    that is always on."""

    events = {event["scope"]: event for event in build_timeline_events(_mixed_sections()) if event["kind"] == "work"}
    assert events["quiet"]["important"] is False and events["quiet"]["salience"] is None
    assert events["failing"]["salience"] == vocabulary.SALIENCE_OWNS_FAILED_CHECK
    assert events["stranded"]["salience"] == vocabulary.SALIENCE_LEFT_IN_PROGRESS
    assert events["broad"]["salience"] == vocabulary.SALIENCE_LARGEST_FILE_SET
    assert sum(1 for event in events.values() if event["important"]) == 3


def test_every_salient_record_says_why_in_the_payload() -> None:
    """A surface must never have to re-derive or guess the reason for a mark."""

    for event in build_timeline_events(_mixed_sections()):
        if not event["important"]:
            assert event["salience"] is None and event["salience_reason"] is None
            continue
        assert event["salience_reason"] == vocabulary.timeline_salience_reason(event["salience"])
        assert event["salience"] in event["salience_keys"]


def test_a_blocker_still_outranks_every_other_reason() -> None:
    task = _mixed_sections()
    stranded = next(item for item in task["work_items"] if item["work_id"] == "stranded")
    stranded["blocker"] = "The staging migration needs an owner role this account does not have."
    event = next(
        row for row in build_timeline_events(task) if row["kind"] == "work" and row["scope"] == "stranded"
    )
    assert event["salience"] == vocabulary.SALIENCE_BLOCKER
    # The weaker reason is still reported; only the headline reason is one.
    assert vocabulary.SALIENCE_LEFT_IN_PROGRESS in event["salience_keys"]


def test_a_completed_step_with_no_check_behind_it_is_pulled_forward() -> None:
    task = _task([{
        "work_id": "unchecked", "section_id": "unchecked", "title": "Add subtract()",
        "client": "claude-code", "client_session_id": "root", "latest_status": "completed",
        "kind": "implementation", "started_at": 10.0, "updated_at": 11.0, "files": ["money.py"],
        "summary": "subtract() landed.", "progress_notes": [],
    }])
    event = next(row for row in build_timeline_events(task) if row["kind"] == "work")
    assert event["evidence_grade"] == "claimed"  # the grade key; the tier word is "unchecked"
    assert event["evidence_grade_label"] == "unchecked"
    assert event["salience"] == vocabulary.SALIENCE_COMPLETED_UNCHECKED


def test_the_largest_file_set_needs_a_task_to_be_largest_in() -> None:
    """One section is not "the largest"; a two-file set is not a large one."""

    lone = _task([{
        "work_id": "solo", "section_id": "solo", "title": "Rename the money module",
        "client": "claude-code", "client_session_id": "root", "latest_status": "completed",
        "kind": "refactor", "started_at": 10.0, "updated_at": 11.0,
        "files": ["a.py", "b.py", "c.py", "d.py"], "summary": "Renamed.", "progress_notes": [],
        "evidence_events": [],
    }])
    event = next(row for row in build_timeline_events(lone) if row["kind"] == "work")
    assert vocabulary.SALIENCE_LARGEST_FILE_SET not in event["salience_keys"]

    small = _mixed_sections()
    for item in small["work_items"]:
        item["files"] = item["files"][:1]
    for event in build_timeline_events(small):
        assert vocabulary.SALIENCE_LARGEST_FILE_SET not in event.get("salience_keys", [])


def test_a_routine_passing_check_is_quiet_and_a_standing_failure_is_not() -> None:
    """``important=True`` for every check is why a 48-record canvas read as 48
    equally loud rows."""

    passed = {
        "event_id": "check_ok", "result": "passed", "name": "subtract() rounds half-up",
        "evidence_type": "test", "created_at": 20.0, "exit_code": 0, "source_type": "client_hook",
        "source": "claude-code", "section_id": "quiet", "check_identity": "check:ok",
        "check_identity_stable": True,
    }
    task = _mixed_sections()
    task["current_check_events"] = [passed, _failed_check("failing", 13.0)]
    task["task_evidence_events"] = [deepcopy(passed), _failed_check("failing", 13.0)]
    checks = {row["event_id"]: row for row in build_timeline_events(task) if row["kind"] == "check"}
    assert checks["check_ok"]["important"] is False
    assert checks["check_failing"]["salience"] == vocabulary.SALIENCE_CURRENT_FAILURE


def test_only_the_latest_run_of_a_check_claims_to_be_a_standing_failure() -> None:
    """A check re-run while it is being narrowed down left every run claiming
    "A recorded failure that no later run has replaced" -- three coral marks in
    the timeline for one check, contradicting the receipt's findings, which read
    one row per identity and correctly showed one. ``supersession_state`` does
    not cover this: it only demotes a failure a later PASS retired.
    """

    first = {
        "event_id": "check_first", "result": "failed", "name": "parse_amount reads accounting negatives",
        "evidence_type": "test", "created_at": 30.0, "exit_code": 1, "source_type": "client_hook",
        "source": "claude-code", "section_id": "parens", "summary": "3 of 7 cases fail: ($12.34) raises instead of returning -12.34",
        "check_identity": "check:parens", "check_identity_stable": True,
    }
    second = {
        **deepcopy(first), "event_id": "check_second", "created_at": 31.0,
        "name": "half-open ($12.34 must not parse",
        "summary": "down to one failure: ($12.34 returns 12.34 where a ValueError is expected",
    }
    task = _task([], checks=[first, second])
    task["task_evidence_events"] = [deepcopy(first), deepcopy(second)]
    rows = {row["event_id"]: row for row in build_timeline_events(task) if row["kind"] == "check"}

    # The earlier run keeps its own label -- that run did fail -- but stops
    # asserting nothing has replaced it.
    assert rows["check_first"]["status_label"] == rows["check_second"]["status_label"]
    assert rows["check_first"]["salience"] is None
    assert rows["check_first"]["important"] is False
    assert rows["check_second"]["salience"] == vocabulary.SALIENCE_CURRENT_FAILURE
    assert rows["check_second"]["important"] is True


def test_an_unstable_check_identity_never_groups_unrelated_failures() -> None:
    """The type fallback ("type:test") is shared by every test check in the
    Task, so grouping runs on it would silence real, separate failures."""

    first = _failed_check("alpha", 30.0)
    second = _failed_check("beta", 31.0)
    for row in (first, second):
        row["check_identity"] = "type:test"
        row["check_identity_stable"] = False
    task = _task([], checks=[first, second])
    task["task_evidence_events"] = [deepcopy(first), deepcopy(second)]
    rows = {row["event_id"]: row for row in build_timeline_events(task) if row["kind"] == "check"}
    assert rows["check_alpha"]["salience"] == vocabulary.SALIENCE_CURRENT_FAILURE
    assert rows["check_beta"]["salience"] == vocabulary.SALIENCE_CURRENT_FAILURE


def test_a_check_that_could_not_run_and_a_repair_run_are_each_named() -> None:
    not_run = {
        "event_id": "check_missing", "result": "error", "name": "grouping test",
        "evidence_type": "test", "created_at": 21.0, "exit_code": 4, "source_type": "client_hook",
        "source": "claude-code", "summary": "file or directory not found: tests/test_money.py",
        "check_identity": "check:missing", "check_identity_stable": True,
    }
    repair = {
        "event_id": "check_repair", "result": "passed", "name": "grouping test",
        "evidence_type": "test", "created_at": 22.0, "exit_code": 0, "source_type": "client_hook",
        "source": "claude-code", "supersedes_check_event_id": "check_failing",
        "check_identity": "check:repair", "check_identity_stable": True,
    }
    task = _task([], checks=[not_run, repair])
    task["task_evidence_events"] = [not_run, repair]
    rows = {row["event_id"]: row for row in build_timeline_events(task) if row["kind"] == "check"}
    assert rows["check_missing"]["salience"] == vocabulary.SALIENCE_CHECK_COULD_NOT_RUN
    assert rows["check_repair"]["salience"] == vocabulary.SALIENCE_RECOVERY_RUN


def test_an_owned_control_record_now_says_why_it_is_marked() -> None:
    """Control records stay salient — agentacct RAN them, so they are the one
    lane that is owned rather than reported — but the bare ``True`` is gone."""

    events = build_timeline_events(
        _task([]), None, {"attempts": [{"attempt_id": "a1", "started_at": 5.0, "execution_state": "succeeded"}]}
    )
    control = next(row for row in events if row["kind"] == "attempt")
    assert control["important"] is True
    assert control["salience"] == vocabulary.SALIENCE_CONTROL_RECORD
    assert control["salience_reason"] == vocabulary.timeline_salience_reason(vocabulary.SALIENCE_CONTROL_RECORD)


# --------------------------------------------------------------------------- #
# E3 — gap ranking and the subagent-silence contradiction                      #
# --------------------------------------------------------------------------- #


def _task_with_silent_subagents() -> dict[str, Any]:
    sessions = [
        {"client": "claude-code", "client_session_id": "root", "session_kind": "root",
         "usage": {"total_tokens": 1000}},
        *[
            {"client": "claude-code", "client_session_id": f"child{index}", "session_kind": "child",
             "usage": {"total_tokens": tokens}}
            for index, tokens in enumerate((1_200_000, 900_000, 296_651), start=1)
        ],
    ]
    item = {
        "work_id": "explore", "section_id": "explore", "title": "Agentacct app exploration",
        "client": "claude-code", "client_session_id": "root", "latest_status": "completed",
        "kind": "research", "started_at": 10.0, "updated_at": 11.0, "files": ["notes.md"],
        "summary": "Read the reducers and the panes.", "progress_notes": [],
    }
    task = _task([item], sessions=sessions)
    task["session_unlinked_work_count"] = 1
    task["actions"] = {
        "tool_category_counts": {"read": 40, "execute": 20},
        "tool_category_total": 60,
        "touched_files": ["a.py"],
        "touched_file_count": 1,
        "action_sources_text": "Hook-captured",
        "capture_known": True,
    }
    return task


def test_gap_rank_puts_unreviewable_work_above_an_unordered_file_list() -> None:
    """Ranked by WHAT THE GAP PREVENTS. Three sessions that burned tokens and
    recorded nothing leave a whole body of work unreadable; an unordered path
    list only weakens work you can still read."""

    assert vocabulary.gap_rank(vocabulary.GAP_CODE_SUBAGENTS_SILENT) < vocabulary.gap_rank(
        vocabulary.GAP_CODE_FILE_OPERATIONS_UNORDERED
    )
    assert vocabulary.gap_rank(vocabulary.GAP_CODE_CAPTURE_COVERAGE) < vocabulary.gap_rank(
        vocabulary.GAP_CODE_DECLARED_PATHS_UNOBSERVED
    )
    # An unrecognised code sorts last rather than first.
    assert vocabulary.gap_rank("something_new") == vocabulary.GAP_RANK_UNRANKED


def test_the_subagent_silence_gap_sorts_above_the_other_reviewer_gaps() -> None:
    gaps = _receipt(_task_with_silent_subagents())["dimensions"]["gaps"]["items"]
    codes = [gap["code"] for gap in gaps]
    assert vocabulary.GAP_CODE_SUBAGENTS_SILENT in codes
    assert codes[0] == vocabulary.GAP_CODE_SUBAGENTS_SILENT
    assert [gap["rank"] for gap in gaps] == sorted(
        gap["rank"] for gap in gaps if gap["kind"] == vocabulary.GAP_KIND_BLOCKS_REVIEW
    ) + [gap["rank"] for gap in gaps if gap["kind"] != vocabulary.GAP_KIND_BLOCKS_REVIEW]


def test_provenance_bookkeeping_still_sorts_last_whatever_its_rank() -> None:
    """Kind wins over rank: a low-rank bookkeeping gap never jumps a
    reviewer-facing one."""

    gaps = _receipt(_task_with_silent_subagents())["dimensions"]["gaps"]["items"]
    kinds = [gap["kind"] for gap in gaps]
    assert vocabulary.GAP_KIND_BOOKKEEPING in kinds
    assert kinds == sorted(kinds, key=lambda kind: 0 if kind == vocabulary.GAP_KIND_BLOCKS_REVIEW else 1)
    bookkeeping = [gap for gap in gaps if gap["kind"] == vocabulary.GAP_KIND_BOOKKEEPING]
    assert any(gap["code"] == vocabulary.GAP_CODE_WORK_NOT_TIED_TO_SESSION for gap in bookkeeping)


def test_every_gap_carries_a_code_even_when_a_dimension_wrote_the_sentence() -> None:
    for gap in _receipt(_task_with_silent_subagents())["dimensions"]["gaps"]["items"]:
        assert gap["code"]
        assert gap["rank"] == vocabulary.gap_rank(gap["code"])


def test_hidden_in_subagents_zero_names_the_contradiction_rather_than_reading_clean() -> None:
    """The field can only count steps a subagent RECORDED. Three subagents that
    recorded nothing therefore produce the same 0 as "no subagent ran" — so the
    0 has to say which of the two it is."""

    strength = _receipt(_task_with_silent_subagents())["axes"]["evidence_strength"]
    assert strength["hidden_in_subagents"] == 0
    assert strength["unrecorded_subagent_sessions"] == 3
    assert strength["unrecorded_subagent_tokens"] == 2_396_651
    assert strength["hidden_in_subagents_text"] == vocabulary.hidden_in_subagents_text(0, 3, 2_396_651)
    assert "2,396,651" in strength["hidden_in_subagents_text"]


def test_a_task_with_no_subagents_at_all_reads_differently_from_a_silenced_one() -> None:
    plain = _receipt(_task([{
        "work_id": "w", "section_id": "w", "title": "Add subtract()", "client": "claude-code",
        "client_session_id": "root", "latest_status": "completed", "kind": "implementation",
        "started_at": 10.0, "updated_at": 11.0, "files": ["money.py"], "summary": "Landed.",
        "progress_notes": [],
    }]))["axes"]["evidence_strength"]
    silenced = _receipt(_task_with_silent_subagents())["axes"]["evidence_strength"]
    assert plain["hidden_in_subagents"] == silenced["hidden_in_subagents"] == 0
    assert plain["hidden_in_subagents_text"] != silenced["hidden_in_subagents_text"]
    assert plain["unrecorded_subagent_sessions"] == 0


def test_the_axes_field_and_the_gap_share_one_derivation() -> None:
    """Two copies of "which subagents recorded nothing" would be free to
    disagree, and the whole point of the pair is that they agree."""

    task = _task_with_silent_subagents()
    sessions, tokens = unrecorded_subagent_sessions(task)
    receipt = _receipt(task)
    strength = receipt["axes"]["evidence_strength"]
    gap = next(
        item
        for item in receipt["dimensions"]["gaps"]["items"]
        if item["code"] == vocabulary.GAP_CODE_SUBAGENTS_SILENT
    )
    assert (strength["unrecorded_subagent_sessions"], strength["unrecorded_subagent_tokens"]) == (sessions, tokens)
    assert gap["reason"] == vocabulary.gap_subagents_recorded_no_work(sessions, tokens)
    assert f"{tokens:,}" in gap["reason"] and f"{tokens:,}" in strength["hidden_in_subagents_text"]


def test_a_subagent_that_did_record_work_is_not_counted_as_silent() -> None:
    task = _task_with_silent_subagents()
    task["work_items"].append({
        "work_id": "child-work", "section_id": "child-work", "title": "Read the panes",
        "client": "claude-code", "client_session_id": "child1", "latest_status": "completed",
        "kind": "research", "started_at": 12.0, "updated_at": 13.0, "files": ["panes.md"],
        "summary": "Read them.", "progress_notes": [],
    })
    sessions, tokens = unrecorded_subagent_sessions(task)
    assert sessions == 2 and tokens == 1_196_651
    strength = _receipt(task)["axes"]["evidence_strength"]
    assert strength["hidden_in_subagents"] == 1
    assert strength["unrecorded_subagent_sessions"] == 2
    # Both facts in one sentence: what the count saw AND what it could not.
    assert "1,196,651" in strength["hidden_in_subagents_text"]


# --------------------------------------------------------------------------- #
# E4 — actions_synopsis after the round-D call shapes                          #
# --------------------------------------------------------------------------- #


def _coverage(*, sections_recorded: int, sections_captured: int, total: int) -> dict[str, Any]:
    return {
        "record_shortfalls": [
            {"record_label": "recorded section", "call_label": "record_section",
             "captured": sections_captured, "recorded": sections_recorded}
        ],
        "captured_first_at": 1_757_700_000.0,
        "captured_last_at": 1_757_701_000.0,
        "activity_first_at": 1_757_600_000.0,
        "activity_last_at": 1_757_900_000.0,
        "total": total,
    }


def test_a_complete_single_batch_capture_still_reads_exact() -> None:
    """Round D changed the required arguments of `record_section` and
    `record_machine_check`. It must not have changed how many CALLS a complete
    session makes, so a capture that saw every one still earns ``exact``."""

    synopsis = vocabulary.actions_synopsis(
        {"read": 20, "edit": 6, "execute": 14},
        40,
        capture_known=True,
        source_text="Hook-captured",
        coverage=_coverage(sections_recorded=4, sections_captured=4, total=40),
    )
    assert synopsis["state"] == "exact"
    assert synopsis["integrity_detail"] is None
    assert vocabulary.ACTIONS_CAPTURE_PARTIAL_QUALIFIER not in (synopsis["tile"]["qualifier"] or "")


def test_a_capture_that_saw_fewer_calls_than_the_ledger_holds_still_reads_partial() -> None:
    synopsis = vocabulary.actions_synopsis(
        {"read": 26, "execute": 25, "agent": 3, "mcp": 5, "other": 1},
        60,
        capture_known=True,
        source_text="Hook-captured",
        coverage=_coverage(sections_recorded=5, sections_captured=4, total=60),
    )
    assert synopsis["state"] == "partial"
    assert vocabulary.GAP_CAPTURE_COVERAGE_PREFIX not in synopsis["integrity_detail"]  # the prefix is the gap's, not the detail's
    assert "5 recorded sections but capture saw 4 record_section calls" in synopsis["integrity_detail"]


def test_a_refused_call_cannot_turn_a_complete_session_partial() -> None:
    """Round D refuses some calls. A refusal writes no record, so the ledger
    holds FEWER records than capture saw calls — the state the shortfall rule
    deliberately does not treat as a shortfall."""

    synopsis = vocabulary.actions_synopsis(
        {"read": 20, "edit": 6, "execute": 14},
        40,
        capture_known=True,
        source_text="Hook-captured",
        coverage=_coverage(sections_recorded=3, sections_captured=5, total=40),
    )
    assert synopsis["state"] == "exact"


# --------------------------------------------------------------------------- #
# end to end: the same facts over a real store and the /v1 lane                #
# --------------------------------------------------------------------------- #


def _section_event(*, section_id: str, status: str, at: float, summary: str | None,
                   files: list[str] | None = None) -> dict[str, Any]:
    return {
        "event_id": f"evt_{section_id}_{status}_{int(at)}",
        "created_at": at,
        "source": "claude-code",
        "event_type": f"section_{status}",
        "run_id": None,
        "metadata": {
            "sentinel_semantic_kind": "section",
            "client": "claude-code",
            "client_session_id": "s1",
            "client_context_keys_authored": ["client_session_id"],
            "project_dir": "/tmp/project",
            "section_id": section_id,
            "section_status": status,
            "section_title": "Add subtract() and format_amount()",
            "kind": "implementation",
            "files": files or [],
            "summary": summary,
            "next_step": "Implement negative sign placement",
        },
    }


def test_the_checkpoint_sentence_reaches_both_v1_payloads(tmp_path) -> None:
    """The original report was a grep over the two live payloads. This is the
    same grep, over a real store and the real routes."""

    from tests.test_receipt_api import _app, _auth
    from agentacct.service import SentinelService

    service = SentinelService(tmp_path)
    service.record_event(_section_event(section_id="money", status="started", at=100.0, summary=None))
    service.record_event(_section_event(section_id="money", status="checkpoint", at=110.0, summary=CHECKPOINT_PROSE))
    service.record_event(
        _section_event(section_id="money", status="completed", at=120.0, summary=CLOSING_PROSE, files=["money.py"])
    )
    client = _app(tmp_path)
    task_id = client.get("/v1/tasks", headers=_auth()).json()["tasks"][0]["task_id"]

    receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth())
    timeline = client.get(f"/v1/task-timeline?task={task_id}&limit=200", headers=_auth())
    assert receipt.status_code == 200 and timeline.status_code == 200
    assert CHECKPOINT_PROSE in receipt.text
    assert CHECKPOINT_PROSE in timeline.text

    beats = [event for event in timeline.json()["events"] if event["kind"] == BEAT_KIND]
    assert [beat["summary"] for beat in beats] == [CHECKPOINT_PROSE]
    section = next(event for event in timeline.json()["events"] if event["kind"] == "work")
    assert beats[0]["section_record_id"] == section["id"]
    # The section's own headline is still the sentence it closed on.
    assert section["summary"] == CLOSING_PROSE
    assert receipt.json()["timeline"]["beat_count"] == 1


def test_truncation_keeps_evidence_even_though_a_passing_check_is_no_longer_salient() -> None:
    """Before salience varied, "keep the important ones" kept every check by
    accident, because every check was important. Kind is now its own reason to
    survive truncation, so making a routine pass quiet cannot delete evidence
    from a long Task."""

    from agentacct.task_intelligence import build_task_intelligence

    passed = {
        "event_id": "check_old", "result": "passed", "name": "subtract() rounds half-up",
        "evidence_type": "test", "created_at": 1.0, "exit_code": 0, "source_type": "client_hook",
        "source": "claude-code", "check_identity": "check:old", "check_identity_stable": True,
    }
    task = _task(
        [
            {"work_id": f"w{index}", "section_id": f"w{index}", "title": f"Step {index}",
             "client": "claude-code", "client_session_id": "root", "latest_status": "completed",
             "kind": "docs", "started_at": float(index + 10), "updated_at": float(index + 10),
             "files": ["doc.md"], "summary": "Wrote it.", "progress_notes": []}
            for index in range(80)
        ],
        checks=[passed],
    )
    result = build_task_intelligence(task, public_task_id="task_beats", title="Large", timeline_limit=20)
    kept = result["timeline"]["events"]
    assert result["timeline"]["truncated"] is True
    check = next(event for event in kept if event["kind"] == "check")
    assert check["important"] is False and check["salience"] is None


def test_a_run_of_salient_sections_cannot_crowd_evidence_out_of_the_window() -> None:
    """The second half of the same trap: once work sections CAN be salient, a
    long run of them would fill the retention budget and push the older checks
    out. Evidence gets first claim on that budget."""

    from agentacct.task_intelligence import build_task_intelligence

    passed = {
        "event_id": "check_old", "result": "passed", "name": "subtract() rounds half-up",
        "evidence_type": "test", "created_at": 1.0, "exit_code": 0, "source_type": "client_hook",
        "source": "claude-code", "check_identity": "check:old", "check_identity_stable": True,
    }
    # 80 completed implementation sections with no check: every one is salient.
    task = _task(
        [
            {"work_id": f"w{index}", "section_id": f"w{index}", "title": f"Step {index}",
             "client": "claude-code", "client_session_id": "root", "latest_status": "completed",
             "kind": "implementation", "started_at": float(index + 10), "updated_at": float(index + 10),
             "files": ["money.py"], "summary": "Landed.", "progress_notes": []}
            for index in range(80)
        ],
        checks=[passed],
    )
    events = build_timeline_events(task)
    salient = [event for event in events if event["kind"] == "work" and event["important"]]
    assert len(salient) == 80  # the crowding condition really is present
    kept = build_task_intelligence(
        task, public_task_id="task_beats", title="Large", timeline_limit=20
    )["timeline"]["events"]
    assert any(event["kind"] == "check" for event in kept)
    # The retained prefix stays in source-time order.
    times = [event["occurred_at"] for event in kept if event["occurred_at"] is not None]
    assert times == sorted(times)
