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

# --- text normalization -----------------------------------------------------

# Whitespace that carries display meaning and therefore survives the control
# sweep below. Tab, newline, vertical tab, form feed and carriage return are all
# category Cc, exactly like the C1 controls the sweep exists to remove -- so the
# exception has to be explicit. (Getting this wrong twice is why the fuzz suite
# asserts both directions: control characters are gone AND line structure
# survives.)
_DISPLAY_MEANINGFUL_WHITESPACE = frozenset("\t\n\v\f\r")

_DISPLAY_LINE_BREAKS = str.maketrans({"\t": " ", "\n": " ", "\r": " ", "\v": " ", "\f": " "})

# Names that carry no identity. Supersession keys on a check's name, so two
# unrelated checks sharing one of these would supersede each other.
GENERIC_CHECK_NAMES = frozenset({"check", "test", "tests", "verify", "build", "run", "lint"})

# A terminal section's outcome prose must be substantial enough to read.
MINIMUM_SUMMARY_CHARACTERS = 40
MINIMUM_BLOCKER_CHARACTERS = 20

TERMINAL_STATUSES = frozenset({"completed", "blocked", "handed_off"})


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


# --- rule checks over a semantic record -------------------------------------


def require_terminal_outcome(
    status: str,
    *,
    summary: Any,
    blocker: Any,
    section_id: str = "",
    source: str = "",
    title: str | None = None,
) -> None:
    """A terminal section must carry the outcome a reader came for (R4).

    A finished chapter with nothing to read is the most visible hole in the
    timeline: the canvas card shows its title plus a status word and nothing
    else. The refusal names the missing field and shows the corrected call, so a
    single retry is enough.
    """
    requirement = {
        "completed": ("summary", MINIMUM_SUMMARY_CHARACTERS, "describe what actually changed and what was verified"),
        "handed_off": ("summary", MINIMUM_SUMMARY_CHARACTERS, "say what is complete and what remains"),
        "blocked": ("blocker", MINIMUM_BLOCKER_CHARACTERS, "state the concrete blocker"),
    }.get(status)
    if requirement is None:
        return
    key, minimum, advice = requirement
    supplied = summary if key == "summary" else blocker
    text = supplied if isinstance(supplied, str) else ""
    # Measure the prose the way it will be stored, once.
    prose = collapse_narrative_text(text)
    if len(prose) >= minimum:
        return

    example_args = [f'source="{source}"', f'section_id="{section_id}"', f'section_status="{status}"']
    if title:
        example_args.append(f'section_title="{collapse_display_text(title)[:60]}"')
    example_args.append(
        f'{key}="<what changed, then what was verified>"'
        if key == "summary"
        else f'{key}="<the concrete blocker>"'
    )
    received = f"{key} of {len(prose)} characters" if prose else f"no {key}"
    raise SemanticRecordError(
        f"section_status={status} requires `{key}` (at least {minimum} characters): {advice}. "
        f"Received: {received}. Re-send the same section_id with that field, for example: "
        f"agentacct_record_section({', '.join(example_args)})."
    )


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
    * a specific check name plus an exit code, which records what ran and what it
      returned even when the exact invocation is not spelled out. Eleven of the
      336 checks in the real ledger have exactly this shape -- an integration
      suite named precisely, with its exit status and no verbatim command -- and
      refusing them would discard genuine evidence.

    The third shape, the before/after outcome lane, is handled by the caller
    before this function is reached, because its evidence is two summaries that
    never appear among these fields.

    What is refused is the record that says nothing: a generic name, no pointer,
    and no exit code.
    """
    if command or files or artifact_ref or artifact_path or artifact_url:
        return
    if exit_code is not None and not is_generic_check_name(name):
        return
    raise SemanticRecordError(
        f"machine check `{name}` (result={result}) records nothing a reviewer can re-run or inspect: "
        "pass `command` (the exact command), `files` (the files it covered), or `artifact_ref`/"
        "`artifact_path`/`artifact_url` (what it produced). A specific `name` with an `exit_code` also "
        "counts. If this was a manual observation, record it with agentacct_record_event instead."
    )


def _supplied(value: Any) -> bool:
    """True when a field carries content, not merely a non-None placeholder.

    An empty string is the shape a fixture builder or an over-eager client
    produces when it means "nothing here", so treating presence as evidence would
    let a check pass reproducibility on a blank field.
    """
    return bool(value.strip()) if isinstance(value, str) else value is not None


def is_generic_check_name(name: Any) -> bool:
    if not isinstance(name, str):
        return True
    stripped = name.strip()
    return len(stripped) < 4 or stripped.lower() in GENERIC_CHECK_NAMES


def require_check_identity(name: Any, *, command: Any = None, files: Any = None) -> None:
    """A check must be identifiable, because supersession keys on its name (R6).

    A specific name identifies it by itself. A generic name is tolerated only
    when something else identifies the check -- a command or a file list.
    """
    if not is_generic_check_name(name):
        return
    if command or files:
        return
    raise SemanticRecordError(
        f"machine check name {name!r} is too generic to identify the check; a later check with the "
        "same name would supersede this one. Use the exact check you ran, for example "
        'name="pytest tests/test_mcp.py" or name="pnpm build:web".'
    )


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
        # A title is required. Measured cost: 534 distinct sections in the real
        # ledger, of which 0 are untitled -- one single event out of 1,168 omits
        # one -- so this refuses nothing an agent actually records. What it
        # prevents is the UI substituting an internal id ("Untitled step -
        # Codex") for a name a reader can use, which is the placeholder the
        # canvas shows when this field is missing.
        if not has_readable_title(title):
            raise SemanticRecordError(
                "section_title is required and must contain readable text (at least 2 letters or "
                "digits), not only whitespace, punctuation or control characters. Use a short name "
                "for this unit of work, for example section_title=\"Add rate-limit to login\"."
            )
        require_terminal_outcome(
            status,
            summary=fields.get("summary"),
            blocker=fields.get("blocker"),
            section_id=str(fields.get("section_id") or ""),
            source=str(fields.get("source") or ""),
            title=collapse_display_text(title) if isinstance(title, str) else None,
        )
        return
    if semantic_kind == "evidence":
        name = fields.get("name")
        # A record that names no check is not a check report: the ledger holds
        # machine-recorded evidence (hook-observed checks, imported activity)
        # that carries a result and nothing else. Identity and reproducibility
        # are only meaningful once there is something to identify.
        if not _supplied(name):
            return
        # The before/after outcome lane records a repair, and the two summaries
        # with their exit codes ARE the evidence -- a shape the CLI and HTTP lanes
        # use deliberately, sometimes carrying only the default check name. An
        # empty string is not evidence, so this tests for content, not presence.
        if _supplied(fields.get("before_summary")) or _supplied(fields.get("after_summary")):
            return
        # Identity before reproducibility: a check called "check" with nothing
        # else cannot be referred to at all, which is more fundamental than a
        # missing pointer and yields the more actionable message.
        require_check_identity(name, command=fields.get("command"), files=fields.get("files"))
        require_reproducible_check(
            name=str(name),
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
