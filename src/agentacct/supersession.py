"""Read-time supersession model for agent-reported machine checks.

A failed/error check that a later *same-scope* passing check contradicts is
demoted out of the pinned "Needs attention" strip into an explicit
``superseded`` state.  It is never hidden, never deleted from the check set,
never upgraded to "Verified", and always reopenable.  Failures that are only
ambiguously contradicted become ``unconfirmed`` and stay standing findings.

This is a retroactive, read-time projection computed where evidence events are
built, BEFORE the raw command/name are redacted out of the projection.  It is
deliberately shaped as a single pairwise gate so a future canonical
``scope_revisions`` / attempt-boundary primitive can REPLACE it rather than be
duplicated into it: nothing here is persisted and nothing infers beyond the
measured gate.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Mapping


SUPERSEDED = "superseded"
UNCONFIRMED = "unconfirmed"

_FAILED_RESULTS = {"failed", "error"}
_CHECK_RESULTS = {"passed", "failed", "error"}

# Basis precedence, strongest first. An explicit agent-declared link is the most
# trustworthy signal; an exact command match beats a shape match beats a name
# match. When several later passes qualify, the strongest basis wins.
_BASIS_RANK = {
    "agent_declared": 3,
    "same_command": 2,
    "command_shape": 1,
    "check_name": 0,
}

_COMMAND_JACCARD_THRESHOLD = 0.8
_NAME_RATIO_THRESHOLD = 0.8

_ALNUM_RUN = re.compile(r"[^\W_]+", re.UNICODE)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (OverflowError, TypeError, ValueError):
        return 0.0


def _result(event: Mapping[str, Any]) -> str:
    return _text(event.get("result")).lower()


def _is_cjk(char: str) -> bool:
    """True for the ideographic / kana / hangul ranges we tokenize per-character."""

    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= code <= 0x4DBF  # CJK Extension A
        or 0x20000 <= code <= 0x2A6DF  # CJK Extension B
        or 0xF900 <= code <= 0xFAFF  # CJK Compatibility Ideographs
        or 0x3040 <= code <= 0x30FF  # Hiragana + Katakana
        or 0xAC00 <= code <= 0xD7A3  # Hangul syllables
    )


def _command_tokens(command: str) -> frozenset[str]:
    """NFKC/casefolded alphanumeric runs, plus each CJK codepoint on its own.

    CJK text has no whitespace between words, so alphanumeric-run splitting alone
    would collapse a whole phrase into one token; adding per-character tokens
    lets Jaccard measure character-level overlap the way it measures word-level
    overlap for space-delimited commands.
    """

    normalized = unicodedata.normalize("NFKC", command).casefold()
    tokens = set(_ALNUM_RUN.findall(normalized))
    tokens.update(char for char in normalized if _is_cjk(char))
    return frozenset(tokens)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _normalized_name(name: str) -> str:
    """Collapsed-whitespace, NFKC/casefolded name for SequenceMatcher."""

    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip().casefold()


def _name_ratio(left: str, right: str) -> float:
    normalized_left = _normalized_name(left)
    normalized_right = _normalized_name(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return difflib.SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _pair_basis(
    failure_command: str | None,
    failure_name: str | None,
    candidate_command: str | None,
    candidate_name: str | None,
    *,
    agent_declared: bool,
) -> str | None:
    """Strongest similarity basis for one (failure, candidate) pair, or None."""

    if agent_declared:
        return "agent_declared"
    if failure_command and candidate_command and failure_command == candidate_command:
        return "same_command"
    if (
        failure_command
        and candidate_command
        and _jaccard(_command_tokens(failure_command), _command_tokens(candidate_command))
        >= _COMMAND_JACCARD_THRESHOLD
    ):
        return "command_shape"
    if (
        failure_name
        and candidate_name
        and _name_ratio(failure_name, candidate_name) >= _NAME_RATIO_THRESHOLD
    ):
        return "check_name"
    return None


def _scope_key(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Pairwise scope identity. A pass in a different project / session / section
    can never supersede: this subsumes any "section maps to one project" guard,
    because a reused section_id across projects has mixed project_identity here.
    """

    return (
        _text(event.get("source")),
        _text(event.get("client")),
        _text(event.get("namespace_fingerprint") or event.get("session_namespace_fingerprint")),
        _text(event.get("project_identity")),
        _text(event.get("section_id")),
        _text(event.get("evidence_type")),
    )


def _order_key(event: Mapping[str, Any]) -> tuple[float, str]:
    return (_num(event.get("created_at")), _text(event.get("event_id")))


def _exit_code_asserts_defect(event: Mapping[str, Any]) -> bool:
    """True only when exit_code is a real non-zero integer.

    An exit-0 failure means the command ran fine and the check is ASSERTING a
    defect; no later command that exits 0 retires it. A missing exit_code is
    equally never demoted -- missing beats wrong.
    """

    exit_code = event.get("exit_code")
    return isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0


def annotate_supersession(
    evidence_events: list[dict[str, Any]],
    *,
    raw_command_by_id: Mapping[str, str | None],
    raw_name_by_id: Mapping[str, str | None],
) -> None:
    """Stamp ``supersession_state`` / ``superseded_by_event_id`` /
    ``supersession_basis`` onto every failed/error evidence event in place.

    Never removes an event from ``evidence_events``: demotion is a state stamp,
    not a deletion, so the failure stays inspectable, keeps its finding episode,
    and can never fall through to a verified outcome.
    """

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for event in evidence_events:
        if not isinstance(event, dict):
            continue
        groups.setdefault(_scope_key(event), []).append(event)

    for group in groups.values():
        ordered = sorted(group, key=_order_key)
        for failure in ordered:
            if _result(failure) not in _FAILED_RESULTS:
                continue
            _stamp_failure(failure, ordered, raw_command_by_id, raw_name_by_id)


def _stamp_failure(
    failure: dict[str, Any],
    ordered_group: list[dict[str, Any]],
    raw_command_by_id: Mapping[str, str | None],
    raw_name_by_id: Mapping[str, str | None],
) -> None:
    # Conservative default: a demotion never happens unless the full gate fires.
    failure["supersession_state"] = None
    failure["superseded_by_event_id"] = None
    failure["supersession_basis"] = None

    if not _exit_code_asserts_defect(failure):
        return

    failure_created = _num(failure.get("created_at"))
    failure_id = _text(failure.get("event_id"))
    failure_command = raw_command_by_id.get(failure_id)
    failure_name = raw_name_by_id.get(failure_id)

    # Same-scope is guaranteed by the group; only the time ordering remains.
    later_events = [
        event for event in ordered_group if _num(event.get("created_at")) > failure_created
    ]
    later_passes = [event for event in later_events if _result(event) == "passed"]
    if not later_passes:
        # No later same-scope pass at all: the finding stands unchanged.
        return

    def basis_for(candidate: Mapping[str, Any]) -> str | None:
        candidate_id = _text(candidate.get("event_id"))
        agent_declared = (
            _result(candidate) == "passed"
            and bool(failure_id)
            and _text(candidate.get("supersedes_check_event_id")) == failure_id
        )
        return _pair_basis(
            failure_command,
            failure_name,
            raw_command_by_id.get(candidate_id),
            raw_name_by_id.get(candidate_id),
            agent_declared=agent_declared,
        )

    gate_passes = [
        (candidate, basis)
        for candidate in later_passes
        if (basis := basis_for(candidate)) is not None
    ]
    if not gate_passes:
        # A later same-scope pass exists, but none is provably the same check.
        failure["supersession_state"] = UNCONFIRMED
        return

    # Regression guard: the most recent later same-scope check that matches the
    # failure by the gate must itself be a PASS, not a re-failure.
    matching_later = [
        candidate
        for candidate in later_events
        if _result(candidate) in _CHECK_RESULTS and basis_for(candidate) is not None
    ]
    most_recent = max(matching_later, key=_order_key)
    if _result(most_recent) != "passed":
        failure["supersession_state"] = UNCONFIRMED
        return

    gate_passes.sort(
        key=lambda pair: (-_BASIS_RANK[pair[1]], _order_key(pair[0])),
    )
    chosen_pass, chosen_basis = gate_passes[0]
    failure["supersession_state"] = SUPERSEDED
    failure["superseded_by_event_id"] = _text(chosen_pass.get("event_id")) or None
    failure["supersession_basis"] = chosen_basis


def is_superseded(event: Mapping[str, Any]) -> bool:
    return _text(event.get("supersession_state")).lower() == SUPERSEDED


def is_unconfirmed(event: Mapping[str, Any]) -> bool:
    return _text(event.get("supersession_state")).lower() == UNCONFIRMED


__all__ = [
    "SUPERSEDED",
    "UNCONFIRMED",
    "annotate_supersession",
    "is_superseded",
    "is_unconfirmed",
]
