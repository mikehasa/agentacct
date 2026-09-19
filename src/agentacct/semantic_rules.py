"""Display-quality and completeness rules for agent-recorded work.

One implementation for every write lane. The MCP tool handlers call these
directly so a refusal reaches the agent as a JSON-RPC error it can act on; the
shared `SentinelService.record_event` choke point calls the same functions so
the HTTP and CLI lanes cannot store a record the MCP lane would refuse.

Design and measurements: ``design-plans/data-quality/RULES.md``. The short
version of why these are refusals rather than quality markers: a record the UI
cannot render is worse than a refusal an agent can fix in one retry, and the
replay of the real ledger (1,504 records) shows zero legitimate reports refused.

Every rule below was validated in both directions -- it refuses the incomplete
record AND accepts every shape the real ledger contains -- because a rule that
is merely strict is not a rule, it is a data-loss bug.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .display_budget import CARD_TITLE_CHARACTERS

# --- text normalization -----------------------------------------------------

# Whitespace that carries display meaning and therefore survives the control
# sweep below. Tab, newline, vertical tab, form feed and carriage return are all
# category Cc, exactly like the C1 controls the sweep exists to remove -- so the
# exception has to be explicit. (Getting this wrong twice is why the fuzz suite
# asserts both directions: control characters are gone AND line structure
# survives.)
_DISPLAY_MEANINGFUL_WHITESPACE = frozenset("\t\n\v\f\r")

_DISPLAY_LINE_BREAKS = str.maketrans({"\t": " ", "\n": " ", "\r": " ", "\v": " ", "\f": " "})

TERMINAL_STATUSES = frozenset({"completed", "blocked", "handed_off"})

# Statuses that end a section by handing it on rather than finishing it. Both
# owe the reader a continuation point, not just a reason.
CONTINUATION_STATUSES = frozenset({"handed_off", "blocked"})

# Check results that assert something went wrong. Both owe a description: a
# reviewer cannot act on a failure whose only text is its own title.
FAILURE_RESULTS = frozenset({"failed", "error"})

#: Section kinds whose steps read and decide rather than change files. These are
#: exactly the four kinds ``task_outcome`` already treats as not check-relevant,
#: so an agent learns one line and not two: a step that owes no check also owes
#: no file anchor. Every other kind -- including ``other`` and ``unknown`` --
#: is expected to name what it touched.
FILE_ANCHOR_EXEMPT_KINDS = frozenset({"planning", "research", "review", "docs"})

#: At most this many advisories travel back on one response. An advisory the
#: agent will not read is noise, and the measured failure was real: a whole
#: recorded session returned exactly one advisory -- a 6-character card-title
#: overrun -- while the same session shipped a section with no files and two
#: indistinguishable checks. Ranking plus a hard cap is what keeps the cosmetic
#: note from outranking the missing anchor.
ADVISORY_RESPONSE_LIMIT = 2


class SemanticRecordError(ValueError):
    """A record cannot be stored because the UI could not render it.

    The message is written for the agent that sent the record: it names the
    field, states what the status requires, and shows a corrected call. It never
    echoes the caller's own text back (see ``_limit_error`` in the MCP lane for
    the same rule and the incident behind it).
    """


#: The control ranges this rule removes: C0 (including DEL) and C1. Everything
#: else -- ordinary text, and the meaningful whitespace named above -- is kept.
_C0_AND_DEL = "\x00-\x08\x0b\x0c\x0e-\x1f\x7f"
_C1 = "\x80-\x9f"
_CONTROL_PATTERN = re.compile(f"[{_C0_AND_DEL}{_C1}]")


def is_control_character(character: str) -> bool:
    """True for a C0/C1 control or DEL. The tabs, newlines and form feeds that
    carry display meaning are NOT controls by this rule's definition.

    The first version of this enumerated the code points it knew about and the
    fuzzer found the hole immediately: U+0080-U+009F (C1 controls) slipped
    through and reached the stored title, where a renderer shows them as nothing
    or as a replacement glyph. Ranges expressed as ranges cannot miss a code
    point the way a hand-written list can.
    """
    if character in _DISPLAY_MEANINGFUL_WHITESPACE:
        return False
    return bool(_CONTROL_PATTERN.fullmatch(character))


def strip_control_characters(value: str) -> str:
    """Drop control characters in one regex pass.

    ``value.translate`` and ``re.sub`` both do their work in C, so this is a
    single scan rather than a Python-level check per character -- which matters
    because it runs on every title, summary, blocker and next_step of every
    record. A clean string returns the same object, so callers that only test for
    cleanliness pay almost nothing.
    """
    if not value:
        return value
    return _CONTROL_PATTERN.sub("", value)


def is_control_free(value: str) -> bool:
    """True when ``value`` holds no C0/C1 control or DEL."""
    return _CONTROL_PATTERN.search(value) is None


def collapse_display_text(value: str) -> str:
    """One line, one space between words, no control characters, no outer space.

    Whitespace is normalized on both display fields and identity fields on
    purpose: "pytest  tests/x.py" and "pytest tests/x.py" name the same check,
    so collapsing them is what keeps supersession -- which keys on the name --
    from treating one check as two.
    """
    return " ".join(strip_control_characters(value.translate(_DISPLAY_LINE_BREAKS)).split())


def collapse_narrative_text(value: str) -> str:
    """Narrative prose: keep real line structure, drop other control characters.

    Line breaks are preserved on purpose. They are how an agent separates a
    summary from the structured text it accidentally absorbed -- a mangled tool
    call arrives as ``...text.</summary>\\n<files>...</files>`` -- and the
    mangled-call detector reads exactly that structure. Collapsing the newline
    would hide the signal the detector exists to find.

    A run of blank lines collapses to one: it renders identically and only makes
    a card taller, so one blank line is the canonical stored form.
    """
    cleaned = strip_control_characters(value.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [" ".join(line.replace("\t", " ").split()) for line in cleaned.split("\n")]
    # A run of blank lines is layout, not signal: keep the first, drop the rest.
    collapsed: list[str] = []
    previous_was_blank = False
    for line in lines:
        if line or not previous_was_blank:
            collapsed.append(line)
        previous_was_blank = not line
    return "\n".join(collapsed).strip()


def readable_text_or_none(value: Any) -> str | None:
    """A single-line display value, or None when nothing readable was supplied."""
    if not isinstance(value, str):
        return None
    collapsed = collapse_display_text(value)
    return collapsed or None


def has_readable_title(value: Any) -> bool:
    """True when a title contains at least two letters or digits.

    `isalnum()` alone would reject readable scripts (Hangul jamo, some combining
    forms), so the check also accepts letter categories.
    """
    collapsed = readable_text_or_none(value)
    if collapsed is None:
        return False
    readable = sum(
        1
        for character in collapsed
        if character.isalnum() or unicodedata.category(character).startswith("L")
    )
    return readable >= 2


# --- what a failure COSTS ----------------------------------------------------
#
# FIELD-VS-SENTENCE, decided and justified here because the page depends on it:
#
#   The BIT is a field. "Does this failure block me?" is the question a reviewer
#   arrives with, and the page has to SORT, GROUP and COLLAPSE on the answer --
#   a receipt already splits its gaps on exactly this axis (`blocks_review`
#   vs `provenance`), and today the reducer has to GUESS which side a failure
#   falls on while the agent knew at write time. A sentence cannot be sorted.
#
#   The SENTENCE is not a new field. A record that owes a cost sentence already
#   has a prose slot for it -- `blocker` on a stopped section, `summary` on a
#   failed check -- and the page's measured disease is too much prose, not too
#   little. Adding a fifth narrative field would have bought one more paragraph
#   for a reviewer to skip.
#
#   Neither is REFUSED. Rendered CLAUDE.md/AGENTS.md instruction files are
#   written once at onboard and never refreshed (the same constraint that keeps
#   the `title` alias alive), so a new refusal on `blocked` would break every
#   already-onboarded agent for a field their instructions never mentioned.
#   The field teaches in its description and advises on omission.
REST_OF_WORK_STATES: tuple[str, ...] = ("usable", "unusable", "unknown")

#: Which state each value claims, in the reviewer's words. One phrase per state,
#: so a surface can print the answer instead of the raw enum key.
REST_OF_WORK_LABELS: dict[str, str] = {
    "usable": "the rest of the work is still usable",
    "unusable": "this blocks the rest of the work",
    "unknown": "whether the rest is usable was not determined",
}

REST_OF_WORK_MISSING_ADVICE = (
    "This record reports a failure and does not say what the failure COSTS. A reviewer "
    "cannot tell from it whether the rest of the work is usable -- and you already know. "
    "Set `rest_of_work` to usable, unusable or unknown. The record is stored as sent."
)


def rest_of_work_state(value: Any) -> str | None:
    """The declared state, or ``None`` when nothing usable was declared. Never
    guessed from the failure itself: a record that did not say stays unsaid."""

    text = str(value or "").strip().lower()
    return text if text in REST_OF_WORK_STATES else None


def rest_of_work_label(value: Any) -> str | None:
    """The reviewer's phrase for a declared state (``None`` when undeclared)."""

    state = rest_of_work_state(value)
    return REST_OF_WORK_LABELS[state] if state is not None else None


def failure_cost_advisory(
    *,
    reports_a_failure: bool,
    rest_of_work: Any,
) -> dict[str, str] | None:
    """Non-blocking advisory for a failure that does not say what it costs.

    ``reports_a_failure`` is the caller's own judgement (a blocked section, a
    failed or errored check), so this rule never has to know either schema.
    """

    if not reports_a_failure or rest_of_work_state(rest_of_work) is not None:
        return None
    return {
        "code": "failure_without_cost",
        "field": "rest_of_work",
        "hint": REST_OF_WORK_MISSING_ADVICE,
    }


# --- the task's goal: what the work was FOR ----------------------------------
#
# Recorded ONCE, on the first started section of a task, and rendered once under
# the title. Without it "completed" is unjudgeable: a reader can see that a step
# finished and still not know what finishing was supposed to achieve.
#
# It is NOT derivable from what the record already holds. Measured across five
# real records, `objectives` is section titles echoed back: objectives[0] equals
# the receipt title on three of five, equals an unrelated section title on a
# fourth, and is empty on the fifth. Section titles are STEPS. A task with 44 of
# them does not have 44 objectives; it has one goal and 44 steps.
TASK_GOAL_ADVICE = (
    "This task has no `goal` on record, so a reader cannot judge what finishing it would "
    "mean. Pass `task_goal` on this section: what you were ASKED to achieve, in the "
    "requester's terms -- 'CSV money columns import without manual cleanup', not 'add "
    "parse_amount()'. Recorded once per task; later sections inherit it."
)


def task_goal_advisory(
    *,
    section_status: Any,
    task_goal: Any,
    goal_recorded_earlier: bool = False,
) -> dict[str, str] | None:
    """Non-blocking advisory about the task-level goal.

    Fires on an OPENING section only (``started``): that is the one call whose
    author still has the request in front of them. A section that inherits a
    goal already on record is never nagged.
    """

    status = str(section_status or "").strip().lower()
    if status != "started" or goal_recorded_earlier or readable_text_or_none(task_goal) is not None:
        return None
    return {"code": "task_without_goal", "field": "task_goal", "hint": TASK_GOAL_ADVICE}


# --- write-time advisories: non-blocking, never a refusal --------------------
# Each advisory is returned in the MCP response next to the stored record. None
# of them changes what is stored, and none can refuse a write: the record has
# already been persisted by the time they are computed.

#: Shared wording for the card-title budget, quoted by every title advisory.
CARD_TITLE_BUDGET_TEXT = (
    f"about {CARD_TITLE_CHARACTERS} characters; lead with the distinguishing words"
)


def _title_budget_advisory(field: str, value: Any) -> dict[str, str] | None:
    text = readable_text_or_none(value)
    if text is None or len(text) <= CARD_TITLE_CHARACTERS:
        return None
    return {
        "code": "title_over_card_budget",
        "field": field,
        "hint": (
            f"`{field}` is {len(text)} characters, longer than a timeline card shows "
            f"({CARD_TITLE_BUDGET_TEXT}). The full text is stored as sent."
        ),
    }


#: Advisory codes ordered by READER impact, most damaging first. Anything not
#: listed sorts after everything listed. The card-title overrun is deliberately
#: last: it costs a reader a few clipped characters, while a duplicate check
#: name costs them the ability to tell two records apart at all.
_ADVISORY_RANK: tuple[str, ...] = (
    # A task with no stated purpose costs the reader every other judgement on
    # the page: they cannot grade "completed" against anything.
    "task_without_goal",
    "duplicate_check_name_in_section",
    "check_without_command_or_artifact",
    "failed_with_exit_code_zero",
    # Below the defects in what WAS recorded: a missing cost leaves a reader
    # undecided, but the record itself is still readable.
    "failure_without_cost",
    "checkpoint_without_next_step",
    "title_over_card_budget",
)


def rank_advisories(advisories: list[dict[str, str]]) -> list[dict[str, str]]:
    """The top ``ADVISORY_RESPONSE_LIMIT`` advisories, worst for the reader first.

    Stable within a rank, so two advisories of equal weight keep the order the
    caller built them in.
    """

    def rank(advisory: dict[str, str]) -> int:
        code = str(advisory.get("code") or "")
        return _ADVISORY_RANK.index(code) if code in _ADVISORY_RANK else len(_ADVISORY_RANK)

    return sorted(advisories, key=rank)[:ADVISORY_RESPONSE_LIMIT]


def section_advisories(
    *,
    section_title: Any,
    section_status: Any = None,
    next_step: Any = None,
    blocker: Any = None,
    task_goal: Any = None,
    goal_recorded_earlier: bool = False,
    rest_of_work: Any = None,
) -> list[dict[str, str]]:
    """Non-blocking advisories for a stored section (never a refusal).

    * an opening section that states no task-level ``task_goal``, so nothing on
      the record says what finishing it would mean.
    * a stopped section that does not say what the stop COSTS a reader.
    * a section still at ``checkpoint`` with no ``next_step``: the one record a
      reader lands on mid-task, with nothing saying where the work stands.
    * a section title longer than the card title budget.
    """

    advisories: list[dict[str, str]] = []
    status = str(section_status or "").strip().lower()
    goal_advisory = task_goal_advisory(
        section_status=status,
        task_goal=task_goal,
        goal_recorded_earlier=goal_recorded_earlier,
    )
    if goal_advisory is not None:
        advisories.append(goal_advisory)
    # A stopped section is a failure report: `blocked` always, and a
    # `handed_off` that recorded a blocker. A `completed` section is not.
    cost_advisory = failure_cost_advisory(
        reports_a_failure=status == "blocked" or (status == "handed_off" and _supplied(blocker)),
        rest_of_work=rest_of_work,
    )
    if cost_advisory is not None:
        advisories.append(cost_advisory)
    if status == "checkpoint" and not _supplied(next_step):
        advisories.append(
            {
                "code": "checkpoint_without_next_step",
                "field": "next_step",
                "hint": (
                    "This section is still open at `checkpoint` and carries no `next_step`. If the "
                    "session stops here, that is the record a reader (or the next session) lands on. "
                    "Add `next_step` with the concrete continuation point. The record is stored as sent."
                ),
            }
        )
    title_advisory = _title_budget_advisory("section_title", section_title)
    if title_advisory is not None:
        advisories.append(title_advisory)
    return rank_advisories(advisories)


def check_advisories(
    *,
    name: Any,
    result: Any,
    exit_code: Any,
    artifact_ref: Any = None,
    artifact_path: Any = None,
    artifact_url: Any = None,
    command: Any = None,
    duplicate_name_in_section: bool = False,
    rest_of_work: Any = None,
) -> list[dict[str, str]]:
    """Non-blocking advisories for a stored machine check (never a refusal).

    Ranked by reader impact and capped, so the cosmetic note can never crowd out
    the one that costs a reader the record:

    * the same check ``name`` already used in this section: two cards a reader
      cannot tell apart. Either distinguish the labels or link the runs with
      ``supersedes_check_event_id``.
    * no ``command`` and no artifact: nothing to re-run and nothing to open.
    * ``result=failed`` with ``exit_code=0`` and no artifact: the command exited
      cleanly and nothing else shows a defect, which is the shape of a probe that
      could not reproduce a problem -- and ``failed`` alone marks the task a
      Finding.
    * a failing check that does not say what the failure COSTS: whether the rest
      of the work is still usable is the question a reviewer opened the record
      to answer, and the agent already knew it at write time.
    * a check name longer than the card title budget.
    """

    advisories: list[dict[str, str]] = []
    no_artifact = not any(_supplied(value) for value in (artifact_ref, artifact_path, artifact_url))
    cost_advisory = failure_cost_advisory(
        reports_a_failure=str(result or "").strip().lower() in FAILURE_RESULTS,
        rest_of_work=rest_of_work,
    )
    if cost_advisory is not None:
        advisories.append(cost_advisory)
    if duplicate_name_in_section:
        advisories.append(
            {
                "code": "duplicate_check_name_in_section",
                "field": "name",
                "hint": (
                    "Another check in this section already carries this `name`, so the two render as "
                    "cards a reader cannot tell apart. If this run re-runs the same check, pass "
                    "`supersedes_check_event_id` with the earlier failed check's event_id (and keep the "
                    "same `check_key`); if it is a different check, give it a `name` that says what THIS "
                    "one proves. The record is stored as sent."
                ),
            }
        )
    if not _supplied(command) and no_artifact:
        advisories.append(
            {
                "code": "check_without_command_or_artifact",
                "field": "command",
                "hint": (
                    "This check records no `command` and no artifact, so a reviewer has nothing to re-run "
                    "and nothing to open. Pass `command` (the exact invocation) or "
                    "`artifact_ref`/`artifact_path`/`artifact_url` (what it produced). The record is "
                    "stored as sent."
                ),
            }
        )
    if result == "failed" and exit_code == 0 and not isinstance(exit_code, bool) and no_artifact:
        advisories.append(
            {
                "code": "failed_with_exit_code_zero",
                "field": "result",
                "hint": (
                    "result=failed was recorded with exit_code=0 and no artifact. `failed` means the check "
                    "shows a defect in the work, and it can mark the task as a Finding. If the check ran "
                    "clean, record passed; if the problem could not be reproduced, record not_reproduced; "
                    "if it was inconclusive for another reason, record unknown; if it could not run, "
                    "record error. The record is stored as sent."
                ),
            }
        )
    title_advisory = _title_budget_advisory("name", name)
    if title_advisory is not None:
        advisories.append(title_advisory)
    return rank_advisories(advisories)


# --- rule checks over a semantic record -------------------------------------


_TERMINAL_REQUIREMENTS: dict[str, tuple[str, str, str]] = {
    "completed": (
        "summary",
        "describe what actually changed and what was verified",
        "<what changed, then what was verified>",
    ),
    "handed_off": (
        "summary",
        "say what is complete and what remains",
        "<what is complete, then what remains>",
    ),
    "blocked": ("blocker", "state the concrete blocker", "<the concrete blocker>"),
}

#: The example value shown for a title the refusal cannot know.
TITLE_PLACEHOLDER = "<a short name for this step, e.g. Add rate-limit to login>"
#: The example value shown for a missing continuation point.
NEXT_STEP_PLACEHOLDER = "<the concrete action that resumes this work>"
#: The example value shown for a missing file anchor. Bracketed on purpose: it
#: must read as a slot to fill, never as a path that could be sent as-is.
FILES_PLACEHOLDER = "<project-relative path this step changed>"
#: Why each continuation status owes a next_step, in the refusal's own words.
_CONTINUATION_REASON = {
    "blocked": (
        "a blocked section says why the work stopped; `next_step` is the only field that says what "
        "would unblock it"
    ),
    "handed_off": (
        "a handed-off section is read by whoever picks the work up, and `next_step` is the only field "
        "that tells them where to start"
    ),
}
#: The example value shown for a required `source` the refusal cannot know.
SOURCE_PLACEHOLDER = "<your client name>"

TITLE_REQUIREMENT = (
    "section_title is required on a section's first record and must contain readable text "
    "(at least 2 letters or digits), not only whitespace, punctuation or control characters"
)


def _quoted(value: str) -> str:
    """A double-quoted argument value that parses back to exactly ``value``."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def inherited_section_title(supplied: Any, recorded: Any) -> str | None:
    """The title a section record inherits when this call omits one.

    A title names the section's identity, not its status, so it is sticky: a
    later record of the SAME section (same client, session and section_id) that
    does not repeat it keeps the latest readable title already on record. Only an
    omitted title inherits -- a supplied one is judged on its own -- and the
    first record of a section has nothing to inherit, so it must carry one.
    """
    if supplied is not None and supplied != "":
        return None
    return readable_text_or_none(recorded) if has_readable_title(recorded) else None


def _missing_terminal_field(status: str, *, summary: Any, blocker: Any) -> tuple[str, str, str] | None:
    requirement = _TERMINAL_REQUIREMENTS.get(status)
    if requirement is None:
        return None
    key = requirement[0]
    supplied = summary if key == "summary" else blocker
    # Judge the prose the way it will be stored: text that collapses to nothing
    # is not a summary.
    if isinstance(supplied, str) and collapse_narrative_text(supplied):
        return None
    return requirement


def section_refusal(
    status: str,
    *,
    title_missing: bool,
    summary: Any,
    blocker: Any,
    section_id: str = "",
    source: str = "",
    title: str | None = None,
    next_step: Any = None,
    kind: Any = None,
    files: Any = None,
    files_recorded_earlier: bool = False,
) -> str | None:
    """Every missing field of a section record, in ONE message with ONE call.

    Refusing one field per round trip turns closing a section into several
    retries, and an agent that gives up leaves a stale open step on the receipt.
    The example call is built from the caller's own normalized arguments --
    never an empty ``source`` and never a shortened title -- so copying it
    verbatim (placeholders filled) is accepted.

    All four section rules are assembled here for exactly that reason: the
    outcome field (R4), the continuation point (D2b) and the file anchor (D2c)
    are three ways a terminal record can be unreadable, and an agent that is told
    about them one at a time makes three calls or gives up on the second.
    """
    terminal = _missing_terminal_field(status, summary=summary, blocker=blocker)
    continuation_missing = status in CONTINUATION_STATUSES and not _supplied(next_step)
    anchor_missing = _missing_file_anchor(
        status, kind=kind, files=files, files_recorded_earlier=files_recorded_earlier
    )
    if not title_missing and terminal is None and not continuation_missing and not anchor_missing:
        return None
    reasons: list[str] = []
    example_args = [
        f"source={_quoted(source or SOURCE_PLACEHOLDER)}",
        f"section_id={_quoted(section_id)}",
        f"section_status={_quoted(status)}",
    ]
    if title_missing:
        reasons.append(TITLE_REQUIREMENT)
        example_args.append(f"section_title={_quoted(TITLE_PLACEHOLDER)}")
    elif title and (status in _TERMINAL_REQUIREMENTS or status in TERMINAL_STATUSES):
        example_args.append(f"section_title={_quoted(collapse_display_text(title))}")
    if terminal is not None:
        key, advice, placeholder = terminal
        reasons.append(f"section_status={status} requires `{key}`: {advice}")
        example_args.append(f"{key}={_quoted(placeholder)}")
    if continuation_missing:
        reasons.append(
            f"section_status={status} requires `next_step`: {_CONTINUATION_REASON[status]}. State the "
            "action, not the background"
        )
        example_args.append(f"next_step={_quoted(NEXT_STEP_PLACEHOLDER)}")
    if anchor_missing:
        exempt = ", ".join(sorted(FILE_ANCHOR_EXEMPT_KINDS))
        reasons.append(
            f"section_status={status} with kind={anchor_missing} requires `files`: they are the only "
            "per-step anchor for what this step changed, and without them the step cannot be tied to "
            "the repository (naming them on any one record of this section is enough). If the step "
            f"changed no files, say so by declaring its `kind` as one of: {exempt} -- never invent a "
            "path to satisfy this rule"
        )
        example_args.append(f'files=["{FILES_PLACEHOLDER}"]')
    fields = "every missing field" if len(reasons) > 1 else "that field"
    return (
        "; ".join(reasons)
        + f". Re-send the same section_id with {fields} in one call, for example: "
        + f"agentacct_record_section({', '.join(example_args)})."
    )


def require_terminal_outcome(
    status: str,
    *,
    summary: Any,
    blocker: Any,
    section_id: str = "",
    source: str = "",
    title: str | None = None,
    next_step: Any = None,
    kind: Any = None,
    files: Any = None,
    files_recorded_earlier: bool = False,
) -> None:
    """A terminal section must carry what a reader came for (R4 + D2b + D2c).

    A finished chapter with nothing to read is the most visible hole in the
    timeline: the canvas card shows its title plus a status word and nothing
    else. Three fields close that hole -- the outcome prose, the continuation
    point on a stopped step, and the files the step touched -- and they are
    refused together, in one message with one corrected call, so a single retry
    is enough.
    """
    message = section_refusal(
        status,
        title_missing=False,
        summary=summary,
        blocker=blocker,
        section_id=section_id,
        source=source,
        title=title,
        next_step=next_step,
        kind=kind,
        files=files,
        files_recorded_earlier=files_recorded_earlier,
    )
    if message is not None:
        raise SemanticRecordError(message)


def require_reproducible_check(
    *,
    name: str,
    result: str,
    command: Any = None,
    files: Any = None,
    exit_code: Any = None,
    artifact_ref: Any = None,
    artifact_path: Any = None,
    artifact_url: Any = None,
) -> None:
    """A machine check must be re-runnable or at least objectively anchored (R5).

    Two shapes satisfy this, in descending order of auditability:

    * a pointer -- `command`, `files`, or an artifact reference/path/url;
    * a check name plus an exit code, which records what ran and what it
      returned even when the exact invocation is not spelled out. Eleven of the
      336 checks in the real ledger have exactly this shape -- an integration
      suite named precisely, with its exit status and no verbatim command -- and
      refusing them would discard genuine evidence.

    The third shape, the before/after outcome lane, is handled by the caller
    before this function is reached, because its evidence is two summaries that
    never appear among these fields.

    What is refused is the record that says nothing: no pointer and no exit
    code.
    """
    if command or files or artifact_ref or artifact_path or artifact_url:
        return
    if exit_code is not None and _supplied(name):
        return
    raise SemanticRecordError(
        f"machine check `{name}` (result={result}) records nothing a reviewer can re-run or inspect: "
        "pass `command` (the exact command), `files` (the files it covered), or `artifact_ref`/"
        "`artifact_path`/`artifact_url` (what it produced). A `name` with an `exit_code` also "
        "counts. If this was a manual observation, record it with agentacct_record_event instead."
    )


def _supplied(value: Any) -> bool:
    """True when a field carries content, not merely a non-None placeholder.

    An empty string is the shape a fixture builder or an over-eager client
    produces when it means "nothing here", so treating presence as evidence would
    let a check pass reproducibility on a blank field.
    """
    return bool(value.strip()) if isinstance(value, str) else value is not None


def require_check_identity(name: Any, *, command: Any = None, files: Any = None) -> None:
    """A check card must say what it is (R6).

    ``name`` is a LABEL for what the check proves, not the check's identity:
    supersession keys on ``check_key`` / (command, evidence_type, section_id),
    so re-running the same command under a rewritten label still supersedes the
    earlier failure. What this rule protects is the reader, not the key -- a card
    with no label and nothing else saying what ran cannot be told from any other.

    A missing label is tolerated only when something else says what ran: a
    command or a file list.
    """
    if _supplied(name) or command or files:
        return
    raise SemanticRecordError(
        "machine check records no `name` and no `command`/`files`, so its card would carry nothing "
        "a reader can identify. Pass `name` (a short label for what this check proves, e.g. "
        'name="percentage() rounds half-up") and `command` (the exact command you ran, e.g. '
        'command="python -m pytest tests/test_percent.py").'
    )


# --- D2: the three refusals that make a record readable ----------------------
# Each one uses the one-call refusal shape that made section summaries reliable:
# it names the rule and shows a corrected call the agent
# can send back verbatim. Measured cost against the installed ledger (1,893
# records replayed through the live write path): (a) 0 refusals -- no stored
# failed/error check lacks a description; (b) 5 refusals; (c) 92 refusals, all
# of them terminal sections of a file-touching kind that named no file anywhere
# in the section. A refusal rejects the CALL and never touches an already-stored
# record.


def require_failure_description(*, result: Any, summary: Any) -> None:
    """D2(a): a failed or error check must describe the failure.

    A reviewer cannot act on a failure with no description: the card already
    shows the label and the verdict, and what they need is the assertion that
    failed and the observed value.
    """

    verdict = str(result or "").strip().lower()
    if verdict not in FAILURE_RESULTS or readable_text_or_none(summary) is not None:
        return
    raise SemanticRecordError(
        f"a check recorded as {verdict} requires `summary`: a reviewer cannot act on a failure with "
        "no description. Give the assertion that failed and the observed vs expected value. "
        'For example: summary="percentage(1, 3) returned 33.33, expected 33.34 (assert round_half_up '
        'failed)".'
    )


def _missing_file_anchor(
    status: str,
    *,
    kind: Any,
    files: Any,
    files_recorded_earlier: bool = False,
) -> str | None:
    """D2(c): the effective kind when a terminal section owes files, else None.

    ``files`` is the only per-step anchor for WHAT CHANGED: without it a finished
    step is a title, a status word and some prose, and nothing ties it to the
    repository. 895 of 1,309 sections store-wide carry none.

    The escape is NAMED, never invented. A step that genuinely changed no files
    declares one of ``FILE_ANCHOR_EXEMPT_KINDS`` -- the same four kinds that owe
    no check -- and the refusal says so, so an agent is never pushed into
    fabricating a path to satisfy a rule. Paths named on any earlier record of
    the same section count, so naming them once is enough.
    """

    if status not in TERMINAL_STATUSES:
        return None
    kind_text = str(kind or "").strip().lower() or "unknown"
    if kind_text in FILE_ANCHOR_EXEMPT_KINDS:
        return None
    if files_recorded_earlier:
        return None
    if isinstance(files, (list, tuple)) and any(
        _supplied(entry) and not _is_example_placeholder(entry) for entry in files
    ):
        return None
    return kind_text


def _is_example_placeholder(value: Any) -> bool:
    """True for a slot copied out of a refusal's example call, e.g. ``<path>``.

    The example call is meant to be copied WITH its placeholders filled. Storing
    an unfilled one as a real path would turn a helpful example into fabricated
    evidence, so an unfilled slot counts as nothing supplied and the same
    refusal comes back.
    """

    text = value.strip() if isinstance(value, str) else ""
    return text.startswith("<") and text.endswith(">")


# --- the single entry point each lane calls ---------------------------------


def validate_semantic_record(
    *,
    semantic_kind: str | None,
    status: str,
    fields: dict[str, Any],
) -> None:
    """Apply every rule that governs an agent-authored semantic record.

    ``semantic_kind`` is the ledger's own discriminator: "section" for a work
    section and "evidence" for a machine check. Anything else -- imported usage,
    a session observation, a finding disposition -- is machine-recorded and is
    deliberately not subject to these rules, because no agent authored it and a
    refusal would drop a fact instead of correcting a report.

    ``status`` is the section status (or the check's result) already normalized by
    the caller, because each lane stores it in a different shape: the MCP lane
    keeps it in metadata, the HTTP lane derives it from the event type.

    Raises ``SemanticRecordError`` with an agent-readable message.

    Cost is measured, not assumed: replaying the real ledger refuses 9 of 1,521
    records (0.59%), every one genuinely incomplete -- no outcome, no pointer and
    no exit code. A rule that refused more would be a data-loss bug, which is why
    each check has a test in both directions.
    """
    if semantic_kind == "section":
        title = fields.get("section_title") or fields.get("title")
        # A title is required on a section's FIRST record. Measured cost: 534
        # distinct sections in the real ledger, of which 0 are untitled -- so
        # this refuses nothing an agent actually records. What it prevents is
        # the UI substituting an internal id ("Untitled step - Codex") for a
        # name a reader can use. Later records of the same section inherit it
        # (``inherited_section_title``; each lane fills it in before calling
        # here), so closing a section never needs the title repeated.
        message = section_refusal(
            status,
            title_missing=not has_readable_title(title),
            summary=fields.get("summary"),
            blocker=fields.get("blocker"),
            section_id=str(fields.get("section_id") or ""),
            source=str(fields.get("source") or ""),
            title=collapse_display_text(title) if isinstance(title, str) else None,
            next_step=fields.get("next_step"),
            kind=fields.get("kind"),
            files=fields.get("files"),
            files_recorded_earlier=bool(fields.get("files_recorded_earlier")),
        )
        if message is not None:
            raise SemanticRecordError(message)
        return
    if semantic_kind == "evidence":
        name = fields.get("name")
        # A record that names no check is not a check report: the ledger holds
        # machine-recorded evidence (hook-observed checks, imported activity)
        # that carries a result and nothing else. Identity and reproducibility
        # are only meaningful once there is something to identify.
        #
        # ``agent_authored_check`` is the exception, set only by the MCP lane an
        # agent calls directly. ``name`` used to default to "check" there, so a
        # nameless agent check was ALWAYS judged; the marker keeps that true now
        # that ``name`` has no default, without extending the rule to records no
        # agent wrote.
        if not _supplied(name) and not fields.get("agent_authored_check"):
            return
        # D2(a): a reviewer cannot act on a failure with no description. Judged
        # before the before/after short-circuit below only for the evidence lane:
        # the outcome lane's two summaries ARE its description.
        if not (_supplied(fields.get("before_summary")) or _supplied(fields.get("after_summary"))):
            require_failure_description(result=fields.get("result"), summary=fields.get("summary"))
        # The before/after outcome lane records a repair, and the two summaries
        # with their exit codes ARE the evidence -- a shape the CLI and HTTP lanes
        # use deliberately, sometimes carrying only the default check name. An
        # empty string is not evidence, so this tests for content, not presence.
        if _supplied(fields.get("before_summary")) or _supplied(fields.get("after_summary")):
            return
        # Identity before reproducibility: a check with no name and nothing
        # else cannot be referred to at all, which is more fundamental than a
        # missing pointer and yields the more actionable message.
        require_check_identity(name, command=fields.get("command"), files=fields.get("files"))
        require_reproducible_check(
            name=str(name) if _supplied(name) else "",
            result=str(fields.get("result") or "unknown"),
            command=fields.get("command"),
            files=fields.get("files"),
            exit_code=fields.get("exit_code"),
            artifact_ref=fields.get("artifact_ref"),
            artifact_path=fields.get("artifact_path"),
            artifact_url=fields.get("artifact_url"),
        )
        return


# Semantic kinds the ledger recognises for agent-authored records.
SECTION_SEMANTIC_KIND = "section"
EVIDENCE_SEMANTIC_KIND = "evidence"
