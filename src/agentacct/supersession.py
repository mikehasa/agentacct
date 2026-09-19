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

import re
import unicodedata
from typing import Any, Mapping


SUPERSEDED = "superseded"
UNCONFIRMED = "unconfirmed"

_FAILED_RESULTS = {"failed", "error"}
_CHECK_RESULTS = {"passed", "failed", "error"}

# Basis precedence, strongest first. An explicit agent-declared link is the most
# trustworthy signal; an exact command match beats a shape match. When several
# later passes qualify, the strongest basis wins.
#
# A name-only basis was deliberately removed: SequenceMatcher on free-text names
# cannot tell sibling checks apart (a passing "test auth logout flow" would
# demote a still-failing "test auth login flow" at ratio 0.88 while their
# commands agree only 0.67, below the shape gate). Demoting a genuinely-open
# failure out of Needs attention on a name coincidence is the exact dishonesty
# this module exists to avoid, and the name arm bought only 2 of 34 demotions on
# the real store. Such pairs now stay "unconfirmed" — visible, with the later
# pass shown inline — which is the honest treatment for "similar name, unproven
# same check".
_BASIS_RANK = {
    "agent_declared": 3,
    "same_command": 2,
    "command_shape": 1,
}

_COMMAND_JACCARD_THRESHOLD = 0.8

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


def _pair_basis(
    failure_command: str | None,
    candidate_command: str | None,
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
    return None


def _scope_key(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Pairwise scope identity. A pass in a different project / section can never
    supersede: this subsumes any "section maps to one project" guard, because a
    reused section_id across projects has mixed project_identity here.

    Session identity is deliberately NOT part of this key. The namespace
    fingerprint here is per-project/org (session_observations), not per-session,
    so two unlinked sessions in one project share a group. That is intentional:
    an EXPLICIT agent-declared link (supersedes_check_event_id) may legitimately
    retire a failure across a linked continuation, and it needs both events in
    the same group to be seen. The per-session guard for the INFERRED bases
    (same_command / command_shape) is applied inside the pairwise gate via
    ``_same_session`` (see #218), so an unlinked different-session pass can no
    longer retire a finding on a coincidental command match.
    """

    return (
        _text(event.get("source")),
        _text(event.get("client")),
        _text(event.get("namespace_fingerprint") or event.get("session_namespace_fingerprint")),
        _text(event.get("project_identity")),
        _text(event.get("section_id")),
        _text(event.get("evidence_type")),
    )


def _same_session(failure: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """True only when both events provably belong to the same client session.

    An INFERRED basis (same_command / command_shape) may retire a finding only
    within the session that raised it (#218). Session identity mirrors the
    ledger's own join: a shared non-empty ``client_session_id`` OR a shared
    non-empty ``client_transcript_id``. When neither side carries any session
    identity the two are NOT provably the same session, so an inferred pass is
    refused -- the honest, safe direction (a finding is never falsely retired).
    """

    fail_session = _text(failure.get("client_session_id"))
    candidate_session = _text(candidate.get("client_session_id"))
    if fail_session and candidate_session and fail_session == candidate_session:
        return True
    fail_transcript = _text(failure.get("client_transcript_id"))
    candidate_transcript = _text(candidate.get("client_transcript_id"))
    if fail_transcript and candidate_transcript and fail_transcript == candidate_transcript:
        return True
    return False


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
            _stamp_failure(failure, ordered, raw_command_by_id)


def _stamp_failure(
    failure: dict[str, Any],
    ordered_group: list[dict[str, Any]],
    raw_command_by_id: Mapping[str, str | None],
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

    # Project / section / type scope is guaranteed by the group; only the time
    # ordering remains here.
    later_events = [
        event for event in ordered_group if _num(event.get("created_at")) > failure_created
    ]

    def _is_agent_declared(candidate: Mapping[str, Any]) -> bool:
        return (
            _result(candidate) == "passed"
            and bool(failure_id)
            and _text(candidate.get("supersedes_check_event_id")) == failure_id
        )

    def basis_for(candidate: Mapping[str, Any]) -> str | None:
        # An EXPLICIT declaration is intentional and may cross a linked
        # continuation (a different session in the same project/section), so it
        # is never gated on session identity.
        if _is_agent_declared(candidate):
            return "agent_declared"
        # The INFERRED bases (same_command / command_shape) only speak to this
        # finding when the pass ran in the SAME session (#218): a coincidental
        # command match from an unlinked different session must not retire it.
        if not _same_session(failure, candidate):
            return None
        return _pair_basis(
            failure_command,
            raw_command_by_id.get(_text(candidate.get("event_id"))),
            agent_declared=False,
        )

    # The only later passes that speak to this finding are those in its own
    # session scope, plus any pass that explicitly declares it superseded. A pass
    # from an unlinked different session is out of scope entirely -- it leaves the
    # finding standing (not even "unconfirmed"), because it never measured this
    # finding's work.
    scoped_later_passes = [
        candidate
        for candidate in later_events
        if _result(candidate) == "passed"
        and (_same_session(failure, candidate) or _is_agent_declared(candidate))
    ]
    if not scoped_later_passes:
        # No later in-scope pass at all: the finding stands unchanged.
        return

    gate_passes = [
        (candidate, basis)
        for candidate in scoped_later_passes
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
