"""The Receipt: one canonical answer to the 8 questions about one Task.

A Receipt is the product's core object — "a local receipt for every coding-agent
task: what ran, what passed, what it cost, and what remains unproven." It is a
projection over ONE converged Task (see ``task_projection``/``task_identity``)
that a person can read in under a minute and know whether to trust, without
reopening the transcript.

It answers eight questions, each carrying its own provenance and gaps:

 1. task       — what was asked, and the boundary of this Task
 2. actors     — which agents / models / subagent sessions did the work
 3. actions    — which KINDS of tools ran, and which artifacts were touched
 4. cost       — tokens and dollars, and on what BASIS that number rests
 5. evidence   — which checks actually ran, with exit codes and scope
 6. outcome    — the decision status (reported / verified / blocked / …)
 7. gaps       — what could NOT be proven (unlinked work, missing checks, …)
 8. provenance — where each field came from (client log / MCP / hook / CI / …)

Two axes are kept deliberately SEPARATE and named explicitly:

  * ``decision_status``  — what a human or agent SAYS happened.
  * ``evidence_strength``— how well that is actually PROVEN.

An agent reporting "done" never raises evidence strength, and a human review or
approval never counts as machine verification (a finding disposition carries
``authoritative_for_check_result=False``). The two are computed from disjoint
inputs so the separation cannot silently erode: evidence strength is derived
ONLY from recorded checks and per-step verification; decision status is derived
from work statuses, blockers, and human dispositions.

This module reuses ``build_task_intelligence`` (states / decision brief /
verification / findings / coverage / lanes / timeline) rather than reinventing
it, and adds the two missing layers M1 requires: a unified per-field provenance
map and a unified gaps block.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .display_vocabulary import (
    ASSERTED_BY_LABELS,
    ASSERTED_BY_PHRASES,
    ATTENTION_REASON_LABELS,
    GAP_LABEL_NOT_YET_PROVEN,
    NOT_CHECK_RELEVANT_DEFINITION,
    NOT_CHECK_RELEVANT_WORDS,
    NOT_GRADEABLE_NO_CHECKABLE_STEPS,
    NOT_GRADEABLE_NO_FINISHED_STEPS,
    NOT_GRADEABLE_NO_STEPS,
    NOT_GRADEABLE_TEXT,
    STILL_OPEN_WORDS,
    STOP_LABELS,
    TASK_GOAL_ABSENT,
    UNCHECKED_STEP_WORDS,
    UNLINKED_CHECK_WORDS,
    asserted_by_label,
    asserted_by_phrase,
    sentence_case,
    CHECK_NOT_RUN_WORDS,
    FAILED_CHECK_RESULTS,
    NOT_RUN_CHECK_RESULTS,
    check_result_label,
    check_result_note,
    check_result_tone,
    more_attention_text,
    COST_LEGEND,
    DECISION_LABELS,
    RECEIPT_FIELD_LABELS,
    SOURCE_LABELS,
    SUPERSEDED_CHECK_DEFINITION,
    TIER_LABELS,
    TIER_TABLE,
    cost_basis_label,
    cost_display,
    decision_label,
    PLAN_SHARE_NOT_REPORTED,
    plan_share_fields,
    plan_share_state_text,
    display_date,
    disposition_effects,
    source_label,
    ACTIONS_NOT_INSTRUMENTED,
    ARTIFACT_PATH_NOT_SHOWN_TEXT,
    ARTIFACT_URL_NOT_SHOWN_TEXT,
    ATTENTION_SORT_TEXT,
    COMMAND_NOT_SHOWN_TEXT,
    DECISION_DEFINITIONS,
    GAP_CAPTURE_COVERAGE_PREFIX,
    GAP_FILE_OPERATIONS_UNORDERED,
    GAP_KIND_BLOCKS_REVIEW,
    GAP_KIND_BOOKKEEPING,
    GAP_CODE_CAPTURE_COVERAGE,
    GAP_CODE_DECLARED_PATHS_UNOBSERVED,
    GAP_CODE_DIMENSION,
    GAP_CODE_FILE_OPERATIONS_UNORDERED,
    GAP_CODE_NO_CHANGE_DESCRIPTION,
    GAP_CODE_NO_COMMIT,
    GAP_CODE_SUBAGENTS_SILENT,
    GAP_CODE_WORK_NOT_TIED_TO_SESSION,
    GAP_NO_CHANGE_DESCRIPTION,
    GAP_NO_COMMIT_RECORDED,
    LIFECYCLE_MARKER_TEXT,
    NOT_CAPTURED_NOUNS,
    REVISION_NOT_CAPTURED,
    TIER_LABELS,
    handoff_marker_line,
    actions_synopsis,
    check_meta_line,
    check_summary_preview,
    checks_heading_line,
    collapse_not_captured_keys,
    command_state_text,
    decision_group,
    not_captured_line,
    gap_declared_paths_unobserved,
    gap_kind_label,
    gap_rank,
    gap_subagents_recorded_no_work,
    hidden_in_subagents_text,
    receipt_field_label,
    related_paths_text,
    revision_contradiction_text,
    revision_label,
    RELATED_PATHS_DEFINITION,
)
from .task_intelligence import build_task_intelligence
from .plural import count_noun
from .task_outcome import (
    EVIDENCE_GRADE_RANK,
    GRADE_CLAIMED,
    GRADE_EXTERNALLY_VERIFIED,
    GRADE_INDEPENDENTLY_CHECKED,
    GRADE_SELF_CHECKED,
    NON_CHECK_RELEVANT_KINDS,
    CHECK_SERIES_RESULTS,
    evidence_event_key,
    finding_check_key,
    latest_task_checks,
    reduce_task_outcome,
    step_evidence_grade,
    step_is_checkable,
    step_verification_counts,
    task_newest_event_at,
    task_session_starts,
)


# frozen: stored/emitted receipts carry this schema string; surfaces pin it.
RECEIPT_SCHEMA_VERSION = "agentacct.receipt.v1"
V1_ATTENTION_SCHEMA_VERSION = "agentacct.v1-attention.v1"

# The user-facing provenance vocabulary — ONE list every surface shares. These
# answer "where did this fact come from", not "how strong is it" (that is the
# evidence axis). ``human`` is first-class here (it is not, in the lower-level
# envelope-authority model, where it is folded into "none").
SOURCE_CLIENT_LOG = "client_log"
SOURCE_MCP = "mcp"
SOURCE_HOOK = "hook"
# Derived by agentacct from the client's OWN transcript/DB at import time, for a
# client whose hook does not fire (Codex from the rollout, OpenCode from the part
# table). Distinct from ``hook``: no live hook observed it — agentacct read it back
# from what the client already recorded on disk.
SOURCE_TRANSCRIPT_SCAN = "transcript_scan"
SOURCE_CI = "ci"
SOURCE_GIT = "git"
SOURCE_HUMAN = "human"
# agentacct's OWN inference from an ambient signal (e.g. a SessionEnd event) —
# not the agent's word, not a check, not a person. The weakest source: it may
# fill a gap honestly but never claims the certainty of a report or a check.
SOURCE_INFERRED = "inferred"
SOURCE_NONE = "none"

# The legend sentences are owned by the shared display vocabulary.
PROVENANCE_LEGEND: dict[str, str] = {
    source: str(SOURCE_LABELS[source]["legend"])
    for source in (
        SOURCE_CLIENT_LOG,
        SOURCE_MCP,
        SOURCE_HOOK,
        SOURCE_TRANSCRIPT_SCAN,
        SOURCE_CI,
        SOURCE_GIT,
        SOURCE_HUMAN,
        SOURCE_INFERRED,
        SOURCE_NONE,
    )
}

# decision_status.key -> who ASSERTS that status. Kept separate from evidence:
# a machine-verified outcome is asserted by the machine; a blocker or a plain
# "done" is an agent report; a reviewed/resolved finding is a human assertion.
_DECISION_ASSERTED_BY: dict[str, str] = {
    "verified": "machine",
    "finding": "machine",
    "finding_superseded": "machine",
    # Every standing failure carries a human "resolved" disposition — the
    # machine fact stays in history; the ATTENTION claim is the human's.
    "finding_resolved_by_user": "human",
    "blocker_resolved_by_user": "human",
    # ``failed`` is refined out of ``blocked`` from an agent-recorded work status,
    # not a machine check — so it is an agent report, exactly like ``blocked``.
    "failed": "agent_report",
    "blocked": "agent_report",
    "reported": "agent_report",
    "resolved": "agent_report",
    "mostly_done": "agent_report",
    "handed_off": "agent_report",
    # Inferred by agentacct from an ambient SessionEnd event — the weakest
    # provenance, deliberately NOT agent_report (the agent never said this).
    "ended_open": "inferred",
    # Inferred by agentacct from the store moving on elsewhere while this Task's
    # open steps recorded nothing finished — the same weakest provenance as
    # ended_open (agentacct's own inference), never a completion or a stated stop.
    "inactive": "inferred",
    "in_progress": "agent_report",
    "observed": "none",
    "unknown": "none",
}

# One statement per decision key, built from the shared definitions (the
# legend, the receipt and task intelligence print the same sentence).
_DECISION_STATEMENTS: dict[str, str] = dict(DECISION_DEFINITIONS)

# Decision labels are owned by the shared display vocabulary (one sentence-case
# label per key; no surface re-cases a raw key). Kept as an alias for readers.
_DECISION_LABELS: dict[str, str] = DECISION_LABELS

# asserted_by labels (chips) and phrases (prose after "asserted by") are owned
# by the shared display vocabulary; re-exported here for existing readers.
_SUCCESS_STATUSES = {"completed", "passed", "resolved"}
# Work statuses that are still OPEN (the step may yet change). Every other
# non-success status is a named terminal stop.
_OPEN_STATUSES = {"started", "checkpoint", "in_progress", ""}
# Terminal stop status -> the ledger words (owned by the display vocabulary).
_STOP_LABELS: dict[str, str] = STOP_LABELS


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _items(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = task.get("work_items") if isinstance(task.get("work_items"), list) else []
    return [row for row in rows if isinstance(row, Mapping)]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# The Actions dimension already CAPTURES every touched artifact path; surfaces
# previewed only the COUNT. This previews a capped slice of the actual paths and
# DISCLOSES the remainder rather than silently truncating (the receipt's honesty
# rule: a bound on coverage is stated, never hidden).
RECEIPT_TOUCHED_FILES_PREVIEW = 12


def touched_files_preview(
    actions: Mapping[str, Any], *, limit: int = RECEIPT_TOUCHED_FILES_PREVIEW
) -> tuple[list[str], int]:
    """Return (paths to show, count elided). Called ONCE, in ``_actions_dimension``,
    which bakes the result into the receipt as ``touched_files_preview`` /
    ``touched_files_elided`` — so every surface (CLI, TUI, macOS app) renders the
    same daemon-provided slice and the cap has a single source of truth."""

    files = [str(path) for path in (actions.get("touched_files") or []) if str(path).strip()]
    shown = files if limit is None or limit < 0 else files[:limit]
    return shown, max(0, len(files) - len(shown))


# The Actions dimension captures every command an execute tool ran; surfaces preview a
# capped slice and disclose the remainder (same honesty rule + single-source-of-truth as
# the touched-file preview). Commands are already single-line + best-effort scrubbed.
RECEIPT_COMMANDS_PREVIEW = 12


def commands_preview(
    actions: Mapping[str, Any], *, limit: int = RECEIPT_COMMANDS_PREVIEW
) -> tuple[list[str], int]:
    """Return (commands to show, count elided). Called ONCE, in ``_actions_dimension``,
    which bakes the result into the receipt as ``commands_preview`` / ``commands_elided``
    so every surface (CLI, TUI, macOS app) renders the same daemon-provided slice."""

    commands = [str(cmd) for cmd in (actions.get("commands") or []) if str(cmd).strip()]
    shown = commands if limit is None or limit < 0 else commands[:limit]
    return shown, max(0, len(commands) - len(shown))


# Tool NAMES are an OPEN set (unlike the 9 fixed categories), so the Actions
# dimension previews the most-used and discloses the remainder — computed ONCE
# here so every surface renders the same ranking, the same way the touched-file
# preview has a single source of truth.
RECEIPT_TOOL_NAMES_PREVIEW = 10


def tool_names_preview(
    actions: Mapping[str, Any], *, limit: int = RECEIPT_TOOL_NAMES_PREVIEW
) -> tuple[list[dict[str, Any]], int]:
    """Return (top tools by count, count of distinct names elided). Sorted by
    count descending, then name ascending for a stable tie-break."""

    counts = actions.get("tool_name_counts")
    counts = counts if isinstance(counts, Mapping) else {}
    ordered = sorted(
        (
            (str(name), int(count))
            for name, count in counts.items()
            if isinstance(count, int) and not isinstance(count, bool) and count > 0
        ),
        key=lambda item: (-item[1], item[0]),
    )
    shown = ordered if limit is None or limit < 0 else ordered[:limit]
    preview = [{"name": name, "count": count} for name, count in shown]
    return preview, max(0, len(ordered) - len(shown))


# --- Evidence axis ------------------------------------------------------------

def _check_source(check: Mapping[str, Any]) -> str:
    from .task_timeline import check_source

    return check_source(check)


def _project_checks(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Shape the Task's latest-per-identity checks for the Evidence dimension."""

    from .finding_disposition import finding_target_digest

    # The surfaced finding-episode index, keyed by target digest, so a failing
    # check row can carry its own disposition handle (state + revision + the
    # digest a /v1 disposition write names). Only SURFACED episodes get a
    # handle — a failure outside this store's scope stays un-disposable here,
    # the same quarantine the write path enforces.
    episodes = (
        task.get("finding_episodes")
        if isinstance(task.get("finding_episodes"), list)
        else []
    )
    episode_by_digest = {
        str(episode.get("target_digest") or ""): episode
        for episode in episodes
        if isinstance(episode, Mapping) and episode.get("target_digest")
    }

    runs_by_identity = _check_runs_by_identity(task)

    shaped: list[dict[str, Any]] = []
    for standing in latest_task_checks(task):
        if not isinstance(standing, Mapping):
            continue
        runs = runs_by_identity.get(finding_check_key(standing, task_scoped=True), [])
        standing_key = evidence_event_key(standing)
        earlier = [run for run in runs if evidence_event_key(run) != standing_key]
        earlier.sort(key=lambda run: _number(run.get("created_at") or run.get("occurred_at")))
        # The reciprocal supersession pointer. The reducer already computes
        # ``superseded_by`` on the failure; without the pointer BACK, a passing
        # run cannot say which failure it fixed and the fail→pass story cannot
        # be rendered from the receipt alone. An agent-declared
        # ``supersedes_check_event_id`` always wins; otherwise the newest
        # earlier FAILURE of this same check identity is the one this pass
        # replaced.
        supersedes = _text(standing.get("supersedes_check_event_id")) or None
        supersedes_basis = "agent_declared" if supersedes else None
        if supersedes is None and _text(standing.get("result")).lower() == "passed":
            failures = [
                run for run in earlier
                if _text(run.get("result")).lower() in FAILED_CHECK_RESULTS
            ]
            if failures:
                supersedes = _text(failures[-1].get("event_id")) or None
                supersedes_basis = "reciprocal_of_supersession" if supersedes else None
        # Earlier runs of the same check are kept, marked superseded, so the
        # receipt carries the whole run history instead of eliding the failure a
        # later pass recovered from. They are emitted BEFORE the standing run,
        # oldest first, so the rows read in the order they happened.
        standing_event_id = _text(standing.get("event_id")) or None
        for run in earlier:
            shaped.append(
                _shape_check(
                    run,
                    runs=runs,
                    episode_by_digest=episode_by_digest,
                    superseded=True,
                    superseded_by_event_id=(
                        _text(run.get("superseded_by_event_id")) or standing_event_id
                    ),
                    supersedes_check_event_id=_text(run.get("supersedes_check_event_id")) or None,
                    supersedes_basis=(
                        "agent_declared" if _text(run.get("supersedes_check_event_id")) else None
                    ),
                    history_run=True,
                )
            )
        shaped.append(
            _shape_check(
                standing,
                runs=runs,
                episode_by_digest=episode_by_digest,
                superseded=_text(standing.get("supersession_state")).lower() == "superseded",
                superseded_by_event_id=_text(standing.get("superseded_by_event_id")) or None,
                supersedes_check_event_id=supersedes,
                supersedes_basis=supersedes_basis,
            )
        )
    return shaped


def _shape_check(
    check: Mapping[str, Any],
    *,
    runs: list[Mapping[str, Any]],
    episode_by_digest: Mapping[str, Any],
    superseded: bool,
    superseded_by_event_id: str | None,
    supersedes_check_event_id: str | None,
    supersedes_basis: str | None,
    history_run: bool = False,
) -> dict[str, Any]:
    """One check row for the Evidence dimension — the standing run or an
    earlier, superseded run of the same check identity."""

    from .finding_disposition import finding_target_digest

    own_key = evidence_event_key(check)
    own_at = _number(check.get("created_at") or check.get("occurred_at"))
    # Runs of this identity that happened BEFORE this row and failed.
    earlier_failed = sum(
        1
        for run in runs
        if evidence_event_key(run) != own_key
        and _number(run.get("created_at") or run.get("occurred_at")) <= own_at
        and _text(run.get("result")).lower() in FAILED_CHECK_RESULTS
    )
    name = check_display_name(check)
    result = _text(check.get("result")).lower() or "unknown"
    summary = check_recorded_summary(check)
    summary_preview, summary_elided = check_summary_preview(summary)
    artifact_path_redacted = check.get("artifact_path_redacted") is True
    artifact_url_redacted = check.get("artifact_url_redacted") is True
    command_redacted = bool(check.get("command_redacted"))
    command_state = _text(check.get("command_state")) or None
    revision = _check_revision(check)
    finding_handle = None
    # A failed check (a finding) and a check that could not run (a named
    # gap) both carry the human-attention handle for their own item. An
    # earlier run kept for history does NOT: a later run of the same check
    # already stands, so the handle belongs to that row alone.
    if not history_run and _text(check.get("result")).lower() in FAILED_CHECK_RESULTS | NOT_RUN_CHECK_RESULTS:
        digest = finding_target_digest(check)
        episode = episode_by_digest.get(str(digest or ""))
        if episode is not None:
            latest = (
                episode.get("latest_disposition")
                if isinstance(episode.get("latest_disposition"), Mapping)
                else {}
            )
            finding_handle = {
                "target_digest": str(digest),
                "state": _text(episode.get("disposition_state")) or "open",
                "revision": int(episode.get("revision") or 0),
                "attention_open": bool(episode.get("attention_open")),
                "note": _text(latest.get("note")) or None,
            }
    files = [_text(path) for path in (check.get("files") if isinstance(check.get("files"), list) else []) if _text(path)]
    # A check that DECLARED files the stamped revision does not contain is a
    # contradiction the record proves against itself: the stamp cannot be the
    # revision the check ran against. The absence check runs at record time
    # (mcp._server_git_context), so a row stamped before it shipped simply
    # carries no verdict here — never a fabricated "all present".
    absent_paths = [
        _text(path)
        for path in (check.get("git_declared_files_absent") or [])
        if _text(path)
    ]
    return (
        {
            "kind": _text(check.get("evidence_type")) or "check",
            # The agent's own short check name (None when none was recorded;
            # the generic "check" counts as none). ``title`` is what every
            # surface prints: name, else the agent's summary, else the type.
            "name": name,
            "title": check_title(check),
            "result": result,
            # The shared words and tone key for the result: surfaces map
            # the tone to a glyph/color and never switch on ``result``.
            "result_label": check_result_label(result),
            "result_tone": check_result_tone(result),
            # ONE line for the row's four facts, composed HERE. Every surface
            # printed ``result_label``, ``exit_code``, ``evidence_type`` and
            # ``source_label`` as four separate words and punctuated them as
            # four sentences -- ``Passed. Exit 0. test. Agent-reported`` -- which
            # reads as four broken fragments rather than one line of four facts.
            # ``_evidence_dimension`` REWRITES this without whatever it hoisted
            # onto the section heading; this value is the un-hoisted default a
            # lone row (or a surface that renders no heading) prints.
            "meta_line": check_meta_line(
                check_result_label(result),
                _int_or_none(check.get("exit_code")),
                _text(check.get("evidence_type")) or None,
                source_label(_check_source(check)),
            ),
            # A named disagreement between result and exit code (display
            # only; the recorded result is never re-graded).
            "note_text": check_result_note(result, check.get("exit_code")),
            "evidence_type": _text(check.get("evidence_type")) or None,
            "exit_code": _int_or_none(check.get("exit_code")),
            "scope": _text(
                check.get("resolution_scope")
                or check.get("project_identity")
                or check.get("project_dir")
            )
            or None,
            "source": _check_source(check),
            # The shared display label for that source (the one vocabulary
            # every surface prints; the app never maps keys itself).
            "source_label": source_label(_check_source(check)),
            "superseded": superseded,
            # An earlier run of this same check, kept so the fail→pass story can
            # be read from the rows. It is NOT the standing run, so it never
            # enters the header tally (which counts the frontier) — it is
            # history the surfaces grey out.
            "history_run": history_run,
            # What "superseded" means, for the row's help (null when the
            # run is current).
            "superseded_definition": SUPERSEDED_CHECK_DEFINITION if superseded else None,
            # Both directions of the supersession link, so a surface can render
            # the fail→pass story from the rows alone: the run that replaced
            # this one, and the run this one replaced.
            "superseded_by_event_id": superseded_by_event_id,
            "supersedes_check_event_id": supersedes_check_event_id,
            "supersedes_basis": supersedes_basis,
            "event_id": _text(check.get("event_id")) or None,
            "at": _number(check.get("created_at") or check.get("occurred_at")) or None,
            # Detail-on-expand fields (additive). The agent's summary,
            # verbatim — None when it recorded none.
            "summary": summary,
            # The summary cut where a SENTENCE ends, never mid-clause: the first
            # sentence whole, then as many further whole sentences as fit the
            # budget. ``summary`` stays verbatim beside it, so a surface
            # offers the rest rather than losing it; ``summary_elided`` says
            # whether there is a rest to offer.
            "summary_preview": summary_preview,
            "summary_elided": summary_elided,
            "files": files,
            "command_redacted": command_redacted,
            # WHICH command state this is, and its own sentence. An
            # agent-supplied command IS stored (the receipt shows the recorded
            # name instead of repeating it); a hook-derived check holds only a
            # sha256 digest and has no text at all. Saying "not stored" for the
            # first was false, and it is the state — not the boolean — that
            # decides the words.
            "command_state": command_state,
            "command_state_text": (
                command_state_text(command_state)
                or (COMMAND_NOT_SHOWN_TEXT if command_redacted else None)
            ),
            # The redaction sentence for each withheld artifact field.
            "artifact_path_state_text": ARTIFACT_PATH_NOT_SHOWN_TEXT if artifact_path_redacted else None,
            "artifact_url_state_text": ARTIFACT_URL_NOT_SHOWN_TEXT if artifact_url_redacted else None,
            "artifact_ref": _text(check.get("artifact_ref")) or None,
            "artifact_url": None if artifact_url_redacted else (_text(check.get("artifact_url")) or None),
            "artifact_url_redacted": artifact_url_redacted,
            # A source's explicit redaction flag wins over a retained value.
            "artifact_path": None if artifact_path_redacted else (_text(check.get("artifact_path")) or None),
            "artifact_path_redacted": artifact_path_redacted,
            # The revision stamped on this check, read mechanically from `git`
            # (never a self-reported SHA). None until a capture path stamps
            # it. The BASIS is load-bearing and the label leads with it: the
            # hook reads HEAD in the same process as the check, while the
            # server reads HEAD when the record ARRIVES — which, for an agent
            # that records before it commits, is the commit BEFORE the work.
            "revision": revision,
            "revision_label": revision_label(revision),
            # Which ``evidence.revision_groups`` entry this row belongs to, and
            # whether that entry's header already prints the label. Set by
            # ``_group_checks_by_revision``; ``revision_label`` itself is kept on
            # the row so a surface that renders no group header still has it.
            "revision_group_index": None,
            "revision_label_hoisted": False,
            # The self-proving contradiction: paths this check declared that do
            # not exist at the revision it was stamped with. CLEARED on the row
            # when several rows of one group carry the byte-identical sentence
            # and the group banner states it once instead -- printing it per row
            # is what put the same sentence verbatim on two rows of
            # task_5f7dbea9.
            "revision_contradiction_text": revision_contradiction_text(
                (revision or {}).get("commit"), absent_paths
            ),
            "revision_absent_files": absent_paths,
            # Run history of this check identity: every recorded run, and
            # how many runs before the one shown failed.
            "runs_total": max(1, len(runs)),
            "earlier_failed": earlier_failed,
            "section_id": _text(check.get("section_id")) or None,
            # The human-attention handle for a surfaced failing check
            # (None on passes and on failures outside this store's scope).
            "finding": finding_handle,
        }
    )


def check_recorded_summary(check: Mapping[str, Any]) -> str | None:
    """The summary the agent wrote, verbatim, or None.

    Released servers filled an omitted ``summary`` with the literal
    ``"<name>: <result>"``. That string is the server's, not the agent's, so a
    stored row carrying exactly it reads as having no summary. The comparison
    is exact on purpose: it inverts one known machine format and makes no
    judgement about prose an agent wrote.
    """

    summary = _text(check.get("summary"))
    if summary == f"{_text(check.get('name'))}: {_text(check.get('result'))}":
        return None
    return summary or None


def check_display_name(check: Mapping[str, Any]) -> str | None:
    """The agent's recorded check name, or None. The placeholder ``check`` --
    what a lane writes when no name was supplied -- is not a name."""

    name = _text(check.get("name"))
    if not name or name.lower() == "check":
        return None
    return name


def check_title(check: Mapping[str, Any]) -> str:
    """The one title every surface prints for a check: its name, else the
    agent's summary, else the command it ran, else its evidence type.

    The command rung exists because the WRITE rule depends on it.
    ``require_check_identity`` lets a check omit ``name`` when it supplies a
    ``command``, on the grounds that the command still says what ran -- but the
    display used to drop straight past the command to the evidence type, so the
    tolerated call rendered as a card titled "test". That is precisely the
    unidentifiable card the rule is there to prevent, so the two now agree: if a
    command is what the writer was allowed to identify the check by, it is what
    the reader is shown. A redacted command is not a title -- it is not the
    agent's text to print -- so that case falls through to the evidence type.
    """

    command = "" if check.get("command_redacted") is True else _text(check.get("command"))
    return (
        check_display_name(check)
        or check_recorded_summary(check)
        or command
        or _text(check.get("evidence_type"))
        or "recorded check"
    )


def _check_runs_by_identity(task: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    """Every recorded run (passed/failed/error) of each Task-scoped check
    identity — the history behind the latest run a receipt shows."""

    runs: dict[str, list[Mapping[str, Any]]] = {}
    seen: set[tuple[str, ...]] = set()
    sources: list[Any] = [task.get("current_check_events"), task.get("task_evidence_events")]
    for item in _items(task):
        sources.extend((item.get("current_check_events"), item.get("evidence_events")))
    for source in sources:
        for event in source if isinstance(source, list) else []:
            if not isinstance(event, Mapping):
                continue
            if _text(event.get("result")).lower() not in CHECK_SERIES_RESULTS:
                continue
            key = evidence_event_key(event)
            if key in seen:
                continue
            seen.add(key)
            runs.setdefault(finding_check_key(event, task_scoped=True), []).append(event)
    return runs


def _check_revision(check: Mapping[str, Any]) -> dict[str, Any] | None:
    """The environment-captured git revision on a check row, or None when no
    capture path stamped one. Turns "a check passed" into "this revision passed
    this check"."""

    basis = _text(check.get("git_revision_basis"))
    commit = _text(check.get("git_commit"))
    if not basis and not commit:
        return None
    return {
        "commit": commit or None,
        "branch": _text(check.get("git_branch")) or None,
        "dirty": check.get("git_dirty") if isinstance(check.get("git_dirty"), bool) else None,
        "basis": basis or "unavailable",
    }


_TIER_BY_GRADE = {
    GRADE_EXTERNALLY_VERIFIED: "externally_verified",
    GRADE_INDEPENDENTLY_CHECKED: "independently_checked",
    GRADE_SELF_CHECKED: "self_checked",
}
# strongest first — the coarse tier used for colour and list sort/filter only.
_TIER_ORDER = ("externally_verified", "independently_checked", "self_checked")


def _step_attached_checks(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The check events attached to ONE step (its projected latest checks, else
    its raw evidence events)."""

    raw = (
        item.get("current_check_events")
        if isinstance(item.get("current_check_events"), list)
        else item.get("evidence_events")
    )
    return [event for event in raw if isinstance(event, Mapping)] if isinstance(raw, list) else []


def _step_passed_check_ids(item: Mapping[str, Any]) -> set[str]:
    """Event-ids of the passing checks linked to ONE step (its projected latest
    checks, else its raw evidence events)."""

    raw = (
        item.get("current_check_events")
        if isinstance(item.get("current_check_events"), list)
        else item.get("evidence_events")
    )
    ids: set[str] = set()
    if isinstance(raw, list):
        for event in raw:
            if not isinstance(event, Mapping):
                continue
            if _text(event.get("result")).lower() != "passed":
                continue
            event_id = _text(event.get("event_id"))
            if event_id:
                ids.add(event_id)
    return ids


def unrecorded_subagent_sessions(task: Mapping[str, Any]) -> tuple[int, int]:
    """``(sessions, tokens)`` for child sessions that recorded no section.

    ONE derivation, shared by the gap that names the silence and by the ledger
    field the silence makes unreadable. Two copies would be free to disagree —
    and the whole point of the pair is that they say the same thing.
    """

    recorded = {
        _text(item.get("client_session_id")) for item in _items(task) if _text(item.get("client_session_id"))
    }
    sessions = 0
    tokens = 0
    for session in task.get("sessions") if isinstance(task.get("sessions"), list) else []:
        if not isinstance(session, Mapping):
            continue
        if _text(session.get("session_kind")) != "child":
            continue
        if _text(session.get("client_session_id")) in recorded:
            continue
        sessions += 1
        spent = _mapping(session.get("usage")).get("total_tokens")
        if isinstance(spent, int) and not isinstance(spent, bool) and spent > 0:
            tokens += spent
    return sessions, tokens


def _evidence_strength(
    task: Mapping[str, Any],
    checks: list[Mapping[str, Any]],
    verification: Mapping[str, Any],
) -> dict[str, Any]:
    """Coverage-first evidence: per-tier RATIOS over the checkable steps, plus an
    honesty LEDGER. There is deliberately NO single collapsed grade word — the
    counts ARE the headline; a categorical label would re-hide exactly the
    variation the ladder exists to show. One coarse ``strongest_tier`` is kept
    for colour + list sort/filter only, never as a headline word.

    Graded from positive proof ONLY (a failing check lands on the decision axis
    as a finding, never here) and from the same disjoint inputs as before, so it
    stays orthogonal to ``decision_status``: an agent's "done" or a human review
    never lifts a tier.
    """

    items = _items(task)
    tiers = {
        "externally_verified": 0,
        "independently_checked": 0,
        "self_checked": 0,
        "unchecked": 0,  # a checkable terminal step with only an agent claim (grade 'claimed')
    }
    not_checkable = 0
    open_or_incomplete = 0
    still_open = 0
    stopped: dict[str, int] = {status: 0 for status in _STOP_LABELS}
    checkable = 0
    # Subagent steps per ledger bucket (tier keys, "not_checkable",
    # "still_open", "stopped_<status>"): a subagent step is always ALSO counted
    # in exactly one bucket, never as a peer of them.
    subagents_by_bucket: dict[str, int] = {}
    # "Hidden in a subagent" means the step ran in a session that is NOT a root —
    # a genuine subagent. A continuation ROOT (the same agent resuming in a new
    # session) is a root, not a subagent, so its steps must not be counted here,
    # or the ledger would falsely say a continued Task's own work "ran in
    # subagents". root_keys lists every root (primary + continuations).
    root_session_ids = {
        _text(ref.get("client_session_id"))
        for ref in (task.get("root_keys") if isinstance(task.get("root_keys"), list) else [])
        if isinstance(ref, Mapping) and _text(ref.get("client_session_id"))
    }
    primary_session = _text(_mapping(task.get("primary_root")).get("client_session_id"))
    if primary_session:
        root_session_ids.add(primary_session)
    hidden_in_subagents = 0

    def _bucket(item: Mapping[str, Any], name: str) -> None:
        nonlocal hidden_in_subagents
        session = _text(item.get("client_session_id"))
        if session and root_session_ids and session not in root_session_ids:
            hidden_in_subagents += 1
            subagents_by_bucket[name] = subagents_by_bucket.get(name, 0) + 1

    for item in items:
        status = _text(item.get("latest_status")).lower()
        attached = _step_attached_checks(item)
        if status not in _SUCCESS_STATUSES:
            stop = status if status in _STOP_LABELS else None
            if stop is not None and attached:
                # A terminal stop that still carries a passing check is graded
                # for the work it did; a stop without positive proof stays a
                # named stop (never "still open", never "unproven").
                grade = step_evidence_grade({**item, "latest_status": "completed"})["grade"]
                tier = _TIER_BY_GRADE.get(grade)
                if tier is not None:
                    checkable += 1
                    tiers[tier] += 1
                    _bucket(item, tier)
                    continue
            open_or_incomplete += 1
            if stop is None:
                still_open += 1
                _bucket(item, "still_open")
            else:
                stopped[stop] += 1
                _bucket(item, f"stopped_{stop}")
            continue
        if not step_is_checkable(item, attached):
            not_checkable += 1
            _bucket(item, "not_checkable")
            continue
        checkable += 1
        grade = step_evidence_grade(item)["grade"]
        tier = _TIER_BY_GRADE.get(grade, "unchecked")
        tiers[tier] += 1
        _bucket(item, tier)

    strongest_tier = next((tier for tier in _TIER_ORDER if tiers[tier]), None)
    checked_total = sum(tiers[tier] for tier in _TIER_ORDER)

    # Unattributed checks: task-pool passing checks linked to no visible step —
    # they still count toward "N passed" but attach to nothing you can see.
    linked: set[str] = set()
    for item in items:
        linked |= _step_passed_check_ids(item)
    task_passing_ids = {
        _text(event.get("event_id"))
        for event in latest_task_checks(task)
        if isinstance(event, Mapping)
        and _text(event.get("result")).lower() == "passed"
        and _text(event.get("event_id"))
    }
    unattributed_checks = len(task_passing_ids - linked)

    # Count passes/failures over the FRONTIER only. A run that a later run of the
    # same check superseded (a fail that a re-run then passed) stays in
    # checks_total and stays visible in the history group, but it must not read
    # as a current failure in the header tally — otherwise "N failed" overstates
    # failures the frontier already cleared, contradicting the row that is marked
    # "superseded by a later passing run". The decision_status lane already
    # excludes superseded checks the same way.
    # Earlier runs kept for history never enter the tally: the tally counts the
    # FRONTIER (the standing run per check identity), which is what it counted
    # before those rows were kept at all.
    frontier = [check for check in checks if not check.get("history_run")]
    passed = sum(
        1
        for check in frontier
        if not check.get("superseded")
        and _text(check.get("result")).lower() == "passed"
    )
    failed = sum(
        1
        for check in frontier
        if not check.get("superseded")
        and _text(check.get("result")).lower() in FAILED_CHECK_RESULTS
    )
    # A check that could not run is neither a pass nor a failure: its own
    # named remainder, and a named evidence gap.
    not_run = sum(
        1
        for check in frontier
        if not check.get("superseded")
        and _text(check.get("result")).lower() in NOT_RUN_CHECK_RESULTS
    )

    superseded = sum(1 for check in frontier if check.get("superseded"))
    earlier_failed = sum(int(check.get("earlier_failed") or 0) for check in frontier)
    unrecorded_subagent_sessions_count, unrecorded_subagent_tokens = unrecorded_subagent_sessions(task)

    # Coarse key kept ONLY for colour + list sort/filter. It is an ordinal, not a
    # headline: undefined < unchecked < self_checked < independently < externally.
    if checkable == 0:
        key = "undefined"
    else:
        key = strongest_tier or "unchecked"

    strength: dict[str, Any] = {
        "key": key,
        "gradeable": checkable > 0,
        "strongest_tier": strongest_tier,
        # the headline — per-tier ratios over checkable steps
        "checkable_total": checkable,
        "checked_total": checked_total,
        "by_tier": dict(tiers),
        # the ledger — what makes the ratio trustworthy
        "not_checkable": not_checkable,
        # Steps outside the ratio because they have not finished successfully
        # (still_open + every ungraded stop); kept for existing readers.
        "open_or_incomplete": open_or_incomplete,
        "still_open": still_open,
        "stopped_blocked": stopped["blocked"],
        "stopped_handed_off": stopped["handed_off"],
        "stopped_failed": stopped["failed"],
        # Subagent steps, each ALSO counted in one bucket above (by bucket).
        "hidden_in_subagents": hidden_in_subagents,
        # This count can only see steps a subagent RECORDED, so a subagent that
        # recorded nothing drives it toward 0 — the reading a reviewer is most
        # likely to take as "nothing is hidden" is produced by the case where
        # everything is. The two fields below are what the 0 is measured
        # against, and the text names the contradiction outright.
        "unrecorded_subagent_sessions": unrecorded_subagent_sessions_count,
        "unrecorded_subagent_tokens": unrecorded_subagent_tokens,
        "hidden_in_subagents_text": hidden_in_subagents_text(
            hidden_in_subagents, unrecorded_subagent_sessions_count, unrecorded_subagent_tokens
        ),
        "subagents_by_bucket": dict(subagents_by_bucket),
        "unattributed_checks": unattributed_checks,
        "total_steps": len(items),
        # check-event tallies + continuity fields for existing readers
        "checks_total": len(frontier),
        "checks_passed": passed,
        "checks_failed": failed,
        "checks_not_run": not_run,
        "checks_superseded": superseded,
        "checks_earlier_failed": earlier_failed,
        "verified_step_count": int(verification.get("verified_step_count") or 0),
        "total_step_count": int(verification.get("total_step_count") or 0),
        "agent_reported_step_count": int(verification.get("agent_reported_step_count") or 0),
        # Wording, not a formula: this line is printed under the coverage headline
        # in the CLI, the TUI and every exported Markdown receipt, and a literal
        # "X of Y" reads as a template the renderer failed to fill.
        "definition": (
            "Counts are passing checks over checkable steps, split by how independent each "
            "check is. These are counts, not a probability of correctness."
        ),
        "tier_legend": [dict(row) for row in TIER_TABLE],
    }
    # The display strings every surface prints (hero, row, tiles, tally), built
    # once here from the counts above.
    strength.update(evidence_display_fields(strength))
    return strength


# The coverage headline + ledger — ONE formatting shared by CLI / TUI / app, so
# no two surfaces can word the same evidence differently (the M1 vocabulary rule).
# Tier words come from the shared tier table ("self-checked", "independently
# checked", ...), the same words its legend defines.
EVIDENCE_TIER_LABEL = {
    "externally_verified": TIER_LABELS["externally_verified"],
    "independently_checked": TIER_LABELS["independently_checked"],
    "self_checked": TIER_LABELS["self_checked"],
}

def not_gradeable_reason(evidence: Mapping[str, Any]) -> str:
    """Why the coverage ratio has a zero denominator, named after the ledger
    bucket that made it zero — never a phrase that denies a fact shown elsewhere
    (a handed-off step with recorded checks is not "no checkable steps")."""

    stopped = {status: int(evidence.get(f"stopped_{status}") or 0) for status in STOP_LABELS}
    stopped_total = sum(stopped.values())
    still_open = int(evidence.get("still_open") or 0)
    if "still_open" not in evidence:
        still_open = max(0, int(evidence.get("open_or_incomplete") or 0) - stopped_total)
    not_checkable = int(evidence.get("not_checkable") or 0)
    total_steps = evidence.get("total_steps")
    total = (
        int(total_steps)
        if isinstance(total_steps, int) and not isinstance(total_steps, bool)
        else stopped_total + still_open + not_checkable
    )
    if total <= 0:
        return NOT_GRADEABLE_NO_STEPS
    if not_checkable and not stopped_total and not still_open:
        return NOT_GRADEABLE_NO_CHECKABLE_STEPS
    if stopped_total and not still_open and not not_checkable:
        words = ", ".join(STOP_LABELS[status] for status, count in stopped.items() if count)
        if stopped_total == 1:
            return f"only step stopped: {words}"
        return f"all {stopped_total} steps stopped: {words}"
    if still_open and not stopped_total and not not_checkable:
        if still_open == 1:
            return f"only step {STILL_OPEN_WORDS}"
        return f"all {still_open} steps {STILL_OPEN_WORDS}"
    return NOT_GRADEABLE_NO_FINISHED_STEPS


def evidence_coverage_headline(evidence: Mapping[str, Any], *, include_unchecked: bool = True) -> str:
    """The coverage ratio, tier by tier — the headline IS the counts, never a
    single collapsed grade word. ``include_unchecked=False`` leaves the unchecked
    count to the gap line that names it (the verdict headline states each fact
    once). The one non-ratio case is ``Not gradeable (<reason>)``."""

    if not evidence.get("gradeable"):
        return f"{sentence_case(NOT_GRADEABLE_TEXT)} ({not_gradeable_reason(evidence)})"
    by_tier = evidence.get("by_tier") or {}
    total = int(evidence.get("checkable_total") or 0)
    parts = [
        f"{int(by_tier[tier])}/{total} {label}"
        for tier, label in EVIDENCE_TIER_LABEL.items()
        if by_tier.get(tier)
    ]
    if not parts:
        parts.append(f"0/{total} checked")
    if include_unchecked and by_tier.get("unchecked"):
        parts.append(f"{int(by_tier['unchecked'])} {UNCHECKED_STEP_WORDS}")
    return " · ".join(parts)


def _coverage_tier_word(evidence: Mapping[str, Any]) -> str:
    """The one tier word a compact coverage form can honestly carry: the tier's
    label when every checked step sits at one tier, else plain ``checked``."""

    by_tier = _mapping(evidence.get("by_tier"))
    present = [tier for tier in EVIDENCE_TIER_LABEL if int(by_tier.get(tier) or 0)]
    if len(present) == 1:
        return EVIDENCE_TIER_LABEL[present[0]]
    return "checked"


def _subagent_suffix(evidence: Mapping[str, Any], bucket: str) -> str:
    count = int(_mapping(evidence.get("subagents_by_bucket")).get(bucket) or 0)
    return f" ({count} in subagents)" if count else ""


def evidence_unproven_parts(evidence: Mapping[str, Any]) -> list[str]:
    """The unproven part of the evidence gap — completed checkable steps with no
    passing check (``1 completed step unchecked``). The only part a ``Not yet
    proven`` label may sit over."""

    unchecked = int(_mapping(evidence.get("by_tier")).get("unchecked") or 0)
    if not unchecked:
        return []
    return [
        f"{count_noun(unchecked, 'completed step')} {UNCHECKED_STEP_WORDS}"
        f"{_subagent_suffix(evidence, 'unchecked')}"
    ]


def evidence_ledger_parts(evidence: Mapping[str, Any]) -> list[str]:
    """What the ratio does NOT cover, part by part and owing no proof claim:
    checks that could not run, still-open steps, named stops (``1 step
    handed off``), checks linked to no step, then the not-check-relevant scope
    count (its definition ships once as ``scope_definition``). Subagent steps
    are never a peer part: each bucket names its share as ``(N in subagents)``."""

    parts: list[str] = []
    not_run = int(evidence.get("checks_not_run") or 0)
    if not_run:
        parts.append(f"{count_noun(not_run, 'check')} {CHECK_NOT_RUN_WORDS}")
    still_open = int(evidence.get("still_open") or 0)
    if "still_open" not in evidence:
        # An older payload without the split buckets: every non-success step.
        still_open = int(evidence.get("open_or_incomplete") or 0)
    if still_open:
        parts.append(f"{count_noun(still_open, 'step')} {STILL_OPEN_WORDS}{_subagent_suffix(evidence, 'still_open')}")
    for status, words in STOP_LABELS.items():
        count = int(evidence.get(f"stopped_{status}") or 0)
        if count:
            parts.append(f"{count_noun(count, 'step')} {words}{_subagent_suffix(evidence, f'stopped_{status}')}")
    unattributed = int(evidence.get("unattributed_checks") or 0)
    if unattributed:
        parts.append(f"{count_noun(unattributed, 'check')} {UNLINKED_CHECK_WORDS}")
    not_checkable = int(evidence.get("not_checkable") or 0)
    if not_checkable:
        parts.append(
            f"{not_checkable} {NOT_CHECK_RELEVANT_WORDS}{_subagent_suffix(evidence, 'not_checkable')}"
        )
    return parts


def evidence_gap_parts(evidence: Mapping[str, Any]) -> list[str]:
    """Every evidence part, most actionable first: the unproven part, then the
    ledger (see :func:`evidence_ledger_parts`)."""

    return [*evidence_unproven_parts(evidence), *evidence_ledger_parts(evidence)]


def evidence_coverage_ledger(evidence: Mapping[str, Any]) -> str:
    """The honest ledger beneath the ratio: what the ratio does NOT cover —
    still-open steps, named stops, checks linked to no step, and steps that are
    not check-relevant (the unproven count is on the gap line)."""

    return " · ".join(evidence_ledger_parts(evidence))


def check_tally_parts(evidence: Mapping[str, Any]) -> list[str]:
    """The ONE check tally, part by part: ``150/156 passed``, ``4 failed``,
    ``2 could not run``, ``1 skipped``, ``1 result not recorded``, ``2
    superseded``, ``1 earlier run failed``. Empty when no check was recorded.
    Both the checks tile and ``check_tally_text`` are built from it, so no
    surface can drop a remainder another surface names."""

    total = int(evidence.get("checks_total") or 0)
    if total == 0:
        return []
    parts = [f"{int(evidence.get('checks_passed') or 0)}/{total} passed"]
    failed = int(evidence.get("checks_failed") or 0)
    if failed:
        parts.append(f"{failed} failed")
    not_run = int(evidence.get("checks_not_run") or 0)
    if not_run:
        parts.append(f"{not_run} {CHECK_NOT_RUN_WORDS}")
    skipped = int(evidence.get("checks_skipped") or 0)
    if skipped:
        parts.append(f"{skipped} skipped")
    unrecorded = int(evidence.get("checks_result_not_recorded") or 0)
    if unrecorded:
        parts.append(f"{unrecorded} {check_result_label('unknown').lower()}")
    superseded = int(evidence.get("checks_superseded") or 0)
    if superseded:
        parts.append(f"{superseded} superseded")
    earlier = int(evidence.get("checks_earlier_failed") or 0)
    if earlier:
        parts.append(f"{count_noun(earlier, 'earlier run')} failed")
    return parts


def check_tally_text(evidence: Mapping[str, Any]) -> str:
    """One check tally with named remainders: ``150/156 passed · 4 failed · 2
    superseded · 1 earlier run failed``; ``no checks recorded`` when none."""

    parts = check_tally_parts(evidence)
    return " · ".join(parts) if parts else "no checks recorded"


def evidence_display_fields(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """The coverage and check strings every surface prints, from the counts:
    ``coverage_hero`` / ``coverage_row`` / ``coverage_tile`` and ``checks_tile``
    / ``check_tally_text`` / ``check_runs_state``. A tile is ``{value, absent,
    qualifier}``: ``value`` is a measured figure only; a zero denominator or no
    checks is a named ``absent`` state (never ``0/0``, never in the metric
    face), and the qualifier is a complete phrase naming the unit."""

    total = int(evidence.get("checkable_total") or 0)
    checked = int(evidence.get("checked_total") or 0)
    coverage_hero = evidence_coverage_headline(evidence)
    if evidence.get("gradeable") and total:
        word = _coverage_tier_word(evidence)
        coverage_row = f"{checked}/{total} {word}"
        # The unit is named: ``self-checked completed steps``; with mixed or no
        # tiers the verb follows the noun (``completed steps checked``).
        qualifier = f"{word} completed steps" if word != "checked" else "completed steps checked"
        coverage_tile = {"value": f"{checked}/{total}", "absent": None, "qualifier": qualifier}
    else:
        coverage_row = NOT_GRADEABLE_TEXT
        coverage_tile = {
            "value": None,
            "absent": NOT_GRADEABLE_TEXT,
            "qualifier": not_gradeable_reason(evidence),
        }

    checks_total = int(evidence.get("checks_total") or 0)
    passed = int(evidence.get("checks_passed") or 0)
    failed = int(evidence.get("checks_failed") or 0)
    not_run = int(evidence.get("checks_not_run") or 0)
    if checks_total == 0:
        checks_tile = {"value": None, "absent": "no checks recorded", "qualifier": None}
        runs_state = "none"
    else:
        # The tile is the tally split at its ratio: ``1/2`` over ``1 passed · 1
        # failed`` — a complete phrase built from the same parts every other
        # surface prints.
        ratio, *remainders = check_tally_parts(evidence)
        value, _, word = ratio.partition(" ")
        checks_tile = {
            "value": value,
            "absent": None,
            "qualifier": " · ".join([f"{passed} {word}", *remainders]),
        }
        runs_state = (
            "failed" if failed else "passed" if passed else "not_run" if not_run else "not_reported"
        )
    return {
        "coverage_hero": coverage_hero,
        "coverage_row": coverage_row,
        "coverage_tile": coverage_tile,
        "checks_tile": checks_tile,
        # What the ratio does not cover, owing no proof claim (``1 step handed
        # off · 2 not check-relevant``); None when nothing is outside it.
        "coverage_ledger": evidence_coverage_ledger(evidence) or None,
        "scope_definition": NOT_CHECK_RELEVANT_DEFINITION,
        "check_tally_text": check_tally_text(evidence),
        "check_runs_state": runs_state,
        # ``2 checks could not run`` — the named gap as its own phrase for a
        # surface that states it beside (never inside) the failure count.
        "checks_not_run_text": f"{count_noun(not_run, 'check')} {CHECK_NOT_RUN_WORDS}" if not_run else None,
    }


# --- Cost -----------------------------------------------------------------------

COST_STATES = ("no_usage", "unpriced", "partial", "complete")


def cost_state(cost: Mapping[str, Any]) -> str:
    """Exactly one cost state: ``no_usage`` (no usage rows), ``unpriced`` (rows,
    nothing priced), ``partial`` (a priced subtotal while some rows are unpriced
    or held) or ``complete``. Absence and partiality never co-occur."""

    stated = _text(cost.get("state"))
    if stated in COST_STATES:
        return stated
    amount = cost.get("estimated_cost_usd")
    if amount is None or isinstance(amount, bool):
        rows = cost.get("rows")
        provenance = cost.get("provenance")
        has_usage = bool(int(rows or 0)) if rows is not None else (
            isinstance(provenance, list) and bool(provenance) and provenance != [SOURCE_NONE]
        )
        return "unpriced" if has_usage else "no_usage"
    return "complete" if cost.get("cost_complete") is True else "partial"


def _cost_display(cost: Mapping[str, Any]) -> dict[str, Any]:
    state = cost_state(cost)
    return cost_display(
        cost.get("estimated_cost_usd"),
        cost.get("cost_complete") is True,
        cost.get("cost_confidence") or cost.get("cost_basis"),
        has_usage=state != "no_usage",
    )


def cost_gap_parts(cost: Mapping[str, Any]) -> list[str]:
    """The cost half of the gap: one named part per non-complete state."""

    state = cost_state(cost)
    if state == "no_usage":
        return ["no usage recorded"]
    if state == "unpriced":
        return ["usage unpriced"]
    if state == "partial":
        return ["cost is a partial subtotal"]
    return []


def cost_display_fields(cost: Mapping[str, Any]) -> dict[str, Any]:
    """``state`` / ``display_text`` / ``basis_label`` / ``legend`` / ``gap_text``
    for one cost object — the strings every surface prints."""

    parts = cost_gap_parts(cost)
    return {
        "state": cost_state(cost),
        "display_text": str(_cost_display(cost)["display_text"]),
        "basis_label": cost_basis_label(cost.get("cost_basis") or cost.get("cost_confidence")),
        "legend": COST_LEGEND,
        "gap_text": " · ".join(parts) if parts else None,
    }


def receipt_cost_text(cost: Mapping[str, Any]) -> str:
    """The Cost line — one dollar grammar shared by every surface
    (:func:`agentacct.display_vocabulary.cost_display`): ``$`` reported/billed,
    ``≈$`` estimate, ``~$`` partial subtotal, always followed by the spelled-out
    cost basis so an estimate can never read as a billed figure. With nothing
    priced the absence is named — ``no usage recorded`` when there were no usage
    rows, else ``unpriced``."""

    shown = _cost_display(cost)
    if shown["prefix"] is None:
        return str(shown["display_text"])
    return f"{shown['display_text']} · {cost_basis_label(cost.get('cost_basis') or cost.get('cost_confidence'))}"


def receipt_category_text(counts: Mapping[str, Any]) -> str:
    """The tool-category summary — ``category×N`` pairs, or ``not instrumented``
    when no hook/transcript categories were captured. Shared by every surface so
    the Actions line reads identically."""

    if not counts:
        return ACTIONS_NOT_INSTRUMENTED
    return " ".join(f"{name}×{value}" for name, value in sorted(counts.items()))


def plan_share_headline(plan_share: Mapping[str, Any] | None) -> str:
    """One honest line for a Task's share of its client's weekly plan.

    Calibrated-or-nothing, the same rule every plan surface honors: a real
    percentage only once the fit is calibrated; otherwise the named state's
    sentence from the one plan-share table (``display_vocabulary``), never a
    fabricated number and never a dash.
    """

    share = plan_share or {}
    if not share.get("calibration_state"):
        return PLAN_SHARE_NOT_REPORTED
    return plan_share_fields(share.get("pct"), share.get("calibration_state"))["headline"]


def _plan_share_with_text(value: Any) -> dict[str, Any] | None:
    share = _mapping(value)
    if not share:
        return None
    return {**share, **plan_share_payload_fields(share)}


def plan_share_payload_fields(plan_share: Mapping[str, Any] | None) -> dict[str, str]:
    """``{chip_text, sentence_text, headline}`` for a Task plan share."""

    share = plan_share or {}
    if not share.get("calibration_state"):
        return plan_share_state_text(None)
    return plan_share_fields(share.get("pct"), share.get("calibration_state"))


# --- Verdict: the one honest line, computed once for every surface ------------
# The receipt LEADS with a verdict that joins the two axes in one sentence —
# what an agent claims (decision) and how well it is proven (evidence) — without
# ever merging them: the decision clause and the proof clause stay separate
# words (and separate colours on the app). Computed in Python so the card, the
# record page, the CLI, the TUI and the exported Markdown read identically.

def verdict_proof_clause(evidence: Mapping[str, Any]) -> str:
    """The proof half of the verdict, sentence-cased, with no decision prefix —
    what a surface that already shows the decision badge prints beside it. The
    shared coverage ratio for a gradeable task (its unchecked count is left to
    the gap line that names it), else ``Not gradeable (<reason>)``."""

    return sentence_case(evidence_coverage_headline(evidence, include_unchecked=False))


def verdict_headline(decision: Mapping[str, Any], evidence: Mapping[str, Any]) -> str:
    """``<decision label> — <proof clause>`` for a surface with no decision
    badge (CLI, Markdown, a copied brief). The proof clause is the shared
    coverage wording (:func:`evidence_coverage_headline`), so the verdict and the
    Evidence dimension word the same counts identically."""

    label = _text(decision.get("label")) or decision_label(decision.get("key"))
    clause = evidence_coverage_headline(evidence, include_unchecked=False)
    if not evidence.get("gradeable"):
        clause = f"{NOT_GRADEABLE_TEXT} ({not_gradeable_reason(evidence)})"
    return f"{label} — {clause}"


def verdict_gap(
    evidence: Mapping[str, Any],
    cost: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The typed verdict gap: ``gap_label`` (``Not yet proven``) + ``gap_text``
    carry ONLY the unproven part (completed checkable steps with no passing
    check); stops, still-open steps, unlinked checks and scope go to
    ``ledger_text`` with no proof label. Cost absence stays in ``gap_cost``."""

    unproven = evidence_unproven_parts(evidence)
    ledger = evidence_ledger_parts(evidence)
    cost_parts = cost_gap_parts(cost) if cost is not None else []
    return {
        "gap_evidence": unproven,
        "gap_cost": cost_parts,
        "gap_label": GAP_LABEL_NOT_YET_PROVEN if unproven else None,
        "gap_text": " · ".join(unproven) if unproven else None,
        "ledger_evidence": ledger,
        "ledger_text": " · ".join(ledger) if ledger else None,
    }


def verdict_gap_line(
    evidence: Mapping[str, Any],
    cost: Mapping[str, Any] | None = None,
) -> str | None:
    """The unproven part, the ledger and the cost gap joined in actionability
    order (the legacy ``verdict.gap``). ``None`` when a task is fully proven,
    fully covered and fully costed."""

    typed = verdict_gap(evidence, cost)
    bits = [*typed["gap_evidence"], *typed["ledger_evidence"], *typed["gap_cost"]]
    return " · ".join(bits) if bits else None


def verdict_health_window(
    task: Mapping[str, Any],
    evidence: Mapping[str, Any],
    canonical: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The time bound on the proof claim: ``Counts since Sep 14``. Bounding the
    counts by the task's own activity window stops a health figure computed over
    months of legacy data from reading as "now". The count itself is stated once,
    in the verdict headline, so this carries the date only (the app puts it on
    the meta line). The date is the LOCAL calendar day. ``None`` when there is
    no datable activity window or nothing checkable."""

    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    starts = [
        _number(s.get("first_activity_at") or s.get("started_at"))
        for s in sessions
        if isinstance(s, Mapping)
    ]
    starts = [value for value in starts if value]
    if not starts:
        return None
    checkable = int(evidence.get("checkable_total") or 0)
    # No checkable step means no proof claim to bound in time, and the verdict
    # headline already names why it is not gradeable.
    if checkable == 0:
        return None
    since_at = min(starts)
    checked = int(evidence.get("checked_total") or 0)
    since_date = display_date(since_at)
    tier_word = _coverage_tier_word(evidence)
    return {
        "since_at": since_at,
        # The local calendar day, ISO form (the same day ``since_date`` names).
        "since_iso": datetime.fromtimestamp(since_at).date().isoformat(),
        "since_date": since_date,
        "proven": checked,
        "checkable": checkable,
        "tier_word": tier_word,
        "text": f"Counts since {since_date}",
    }


# --- Decision axis ------------------------------------------------------------

def _decision_status(
    task: Mapping[str, Any],
    *,
    latest_store_activity_at: float | None,
    session_starts: Mapping[str, float] | None = None,
    canonical: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if canonical is None:
        canonical = reduce_task_outcome(
            task,
            latest_store_activity_at=latest_store_activity_at,
            session_starts=session_starts,
        )
    key = _text(canonical.get("key")) or "observed"

    # Refine ``failed`` out of ``blocked`` at the Receipt layer without changing
    # the task_outcome contract: a step recorded literally ``failed`` with no
    # blocker text is a failure, not an agent-declared blocker.
    if key == "blocked":
        statuses = [_text(item.get("latest_status")).lower() for item in _items(task)]
        has_failed = any(status == "failed" for status in statuses)
        has_blocker = any(_text(item.get("blocker")) for item in _items(task))
        if has_failed and not has_blocker:
            key = "failed"

    attention = _text(canonical.get("finding_attention_state"))
    # Every standing failure human-resolved -> a DISTINCT decision word, so
    # every surface (app groups/tints, TUI, CLI) files the task done-ish by
    # vocabulary alone. The failing checks stay in the evidence dimension;
    # this never touches evidence strength.
    if key == "finding" and attention == "resolved":
        key = "finding_resolved_by_user"
    asserted_by = _DECISION_ASSERTED_BY.get(key, "none")
    # A finding a human has reviewed (but not resolved) stays a red finding
    # with a HUMAN assertion.
    if key == "finding" and attention == "reviewed":
        asserted_by = "human"

    statement = _DECISION_STATEMENTS.get(key, _DECISION_STATEMENTS["unknown"])
    if key == "finding" and attention == "reviewed":
        statement = "Every current finding was reviewed; no passing check has replaced the failed evidence."

    # The newest blocker's own words (agent-recorded text, next step, when, and
    # how many successful steps were recorded AFTER it) — computed by the
    # outcome reducer, passed through verbatim so every surface can finally SAY
    # why a Task is blocked instead of only that it is. Present on the
    # blocked/failed keys and on ``blocker_resolved_by_user`` (which carries a
    # resolved blocker so its callout stays reachable for reopen); None
    # otherwise.
    blocker = canonical.get("blocker") if isinstance(canonical.get("blocker"), Mapping) else None

    return {
        "key": key,
        "label": decision_label(key),
        "statement": statement,
        "asserted_by": asserted_by,
        "asserted_by_label": asserted_by_label(asserted_by),
        "asserted_by_phrase": asserted_by_phrase(asserted_by),
        "finding_attention_state": attention or None,
        "blocker": blocker,
    }


def _handoff_marker(canonical: Mapping[str, Any]) -> dict[str, Any]:
    """The handoff LIFECYCLE marker — a signal kept deliberately SEPARATE from the
    decision word, alongside the two axes.

    A Task can read ``finding`` (a red check you must act on) AND still have been
    cleanly handed off: the decision word carries the louder problem, this marker
    carries the deliberate stop, and neither hides the other. ``handed_off`` here
    is the recency-aware disposition from ``reduce_task_outcome`` — true only when
    the handoff is the frontier (nothing still-open is newer than it), so a Task
    that was handed off and then RESUMED does not carry the marker. Surfaces show
    the chip only when it adds information the decision word does not already state
    (i.e. ``handed_off`` is true AND ``decision_status.key != "handed_off"``)."""

    handed_off = bool(canonical.get("handoff_current"))
    statement = _DECISION_STATEMENTS["handed_off"] if handed_off else None
    return {
        "handed_off": handed_off,
        "statement": statement,
        # The rendered one-line marker, so no surface re-derives these words.
        "marker_line": handoff_marker_line(handed_off, statement),
        "asserted_by": "agent_report",
    }


def _asserted_by_source(asserted_by: str, checks: list[Mapping[str, Any]]) -> str:
    if asserted_by == "human":
        return SOURCE_HUMAN
    if asserted_by == "machine":
        sources = {check.get("source") for check in checks}
        if SOURCE_HOOK in sources:
            return SOURCE_HOOK
        if SOURCE_CI in sources:
            return SOURCE_CI
        return SOURCE_MCP
    if asserted_by == "agent_report":
        return SOURCE_MCP
    if asserted_by == "inferred":
        return SOURCE_INFERRED
    return SOURCE_NONE


# --- Dimension builders -------------------------------------------------------

def _boundary(task: Mapping[str, Any]) -> dict[str, Any]:
    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    project = next(
        (_text(session.get("project")) for session in sessions if isinstance(session, Mapping) and _text(session.get("project"))),
        None,
    )
    project_identity_state = next(
        (
            _text(session.get("project_identity_state"))
            for session in sessions
            if isinstance(session, Mapping) and _text(session.get("project_identity_state"))
        ),
        None,
    )
    namespace_scope = next(
        (
            _text(session.get("identity_scope_state"))
            for session in sessions
            if isinstance(session, Mapping) and _text(session.get("identity_scope_state"))
        ),
        None,
    )
    # An explicitly identified project binds the Task even when it carries no
    # cryptographic org-namespace fingerprint (only codex / org-scoped sessions
    # have one). Report the strongest binding, so a claude-code Task in a known
    # project reads as "project"-scoped rather than "unscoped".
    if project_identity_state == "explicit":
        identity_scope = "project"
    elif namespace_scope:
        identity_scope = namespace_scope
    else:
        identity_scope = "unscoped"
    root_keys = task.get("root_keys") if isinstance(task.get("root_keys"), list) else []
    return {
        "primary_root": _mapping(task.get("primary_root")) or None,
        "root_count": len(root_keys),
        "session_count": int(task.get("session_count") or 0),
        "is_continuation": bool(task.get("continuation_id")),
        "project": project,
        "project_identity_state": project_identity_state or None,
        "identity_scope": identity_scope,
        "gap_text": boundary_gap_text(project, project_identity_state),
    }


def boundary_gap_text(project: Any, project_identity_state: Any) -> str | None:
    """The boundary gap, derived from the same fields the boundary display
    shows — never "could not be bound" beside a named project."""

    state = _text(project_identity_state)
    if state == "conflicting":
        return "Sessions in this Task report different projects."
    if not _text(project):
        return "No project recorded for this Task."
    if state != "explicit":
        return "Project inferred from session paths, not declared."
    return None


def session_identity_source(task: Mapping[str, Any]) -> str:
    """Where this Task's session identity actually came from — named from the
    rows that were read, never assumed: ``client_log`` only when usage rows
    were imported from the client's own log, ``hook`` when an agentacct client
    hook supplied the session context, else ``mcp`` (the agent reported its own
    session id through agentacct's MCP tools)."""

    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    usage_rows = sum(
        int(_mapping(session.get("usage")).get("rows") or 0)
        for session in sessions
        if isinstance(session, Mapping)
    )
    if usage_rows > 0 or int(_mapping(task.get("usage")).get("rows") or 0) > 0:
        return SOURCE_CLIENT_LOG
    if any(_text(item.get("client_context_source")) == "claude_code_hook" for item in _items(task)):
        return SOURCE_HOOK
    return SOURCE_MCP


_SESSION_IDENTITY_GAPS: dict[str, str] = {
    SOURCE_MCP: "Session identity was not observed in a client log; it comes from the agent's own report.",
    SOURCE_HOOK: "Session identity was not observed in a client log; it comes from a client hook.",
}


def _task_dimension(task: Mapping[str, Any], title: str) -> dict[str, Any]:
    objectives: list[str] = []
    seen: set[str] = set()
    # The task-level GOAL, recorded once by the agent on the first section of a
    # task (`task_goal`). It is not derivable from anything else on the record:
    # `objectives` below is the list of SECTION TITLES, which are steps, and on
    # most records objectives[0] is the Task title again. So the goal is read
    # verbatim from whichever recorded section carried it, and a task with none
    # says so — `goal_absent_text` is one of the two absences the record page's
    # absence budget exempts, because a record with no stated purpose is one a
    # reviewer should distrust.
    goal: str | None = None
    for item in _items(task):
        candidate = _text(item.get("task_goal"))
        if candidate:
            goal = candidate
            break
    for item in _items(task):
        objective = _text(item.get("objective") or item.get("title") or item.get("summary"))
        if objective and objective not in seen:
            seen.add(objective)
            objectives.append(objective)
    boundary = _boundary(task)
    gaps: list[str] = []
    absences: list[dict[str, str]] = []
    if not objectives:
        gaps.append("No explicit objective was recorded for this Task.")
    # Gapped when the Task is not bound to a declared project or namespace (or
    # its sessions disagree), worded from the boundary fields themselves.
    if boundary["gap_text"] and (
        boundary["identity_scope"] == "unscoped" or boundary["project_identity_state"] == "conflicting"
    ):
        gaps.append(boundary["gap_text"])
        absences.append({"key": "project", "text": boundary["gap_text"]})
    provenance = [SOURCE_MCP] if objectives else []
    if boundary["session_count"]:
        provenance.append(session_identity_source(task))
    return {
        "title": title,
        "goal": goal,
        "goal_absent_text": None if goal else TASK_GOAL_ABSENT,
        "objectives": objectives,
        "boundary": boundary,
        "provenance": sorted(set(provenance)) or [SOURCE_NONE],
        "gaps": gaps,
        "absences": absences,
    }


def _actors_dimension(task: Mapping[str, Any], intelligence: Mapping[str, Any]) -> dict[str, Any]:
    lanes = intelligence.get("lanes") if isinstance(intelligence.get("lanes"), list) else []
    models = list(intelligence.get("models") or ())
    primary = next((lane for lane in lanes if isinstance(lane, Mapping) and lane.get("role") == "primary"), None)
    supporting = [lane for lane in lanes if isinstance(lane, Mapping) and lane.get("role") == "supporting"]
    gaps: list[str] = []
    absences: list[dict[str, str]] = []
    if not models:
        gaps.append("No model was observed for this Task's usage.")
        absences.append({"key": "model", "text": gaps[-1]})
    if supporting and not any(lane.get("session_kinds") for lane in supporting):
        gaps.append("Subagent roles were not scanned, so supporting sessions show as counts only.")
    session_count = int(task.get("session_count") or 0)
    source = session_identity_source(task) if session_count or lanes else SOURCE_NONE
    if source in _SESSION_IDENTITY_GAPS:
        gaps.append(_SESSION_IDENTITY_GAPS[source])
        absences.append({"key": "session_identity", "text": gaps[-1]})
    return {
        "primary_agent": _text(primary.get("client")) if isinstance(primary, Mapping) else None,
        "models": models,
        "lanes": lanes,
        "session_count": session_count,
        "subagent_session_count": int(task.get("supporting_count") or 0),
        "child_session_count": int(task.get("child_count") or 0),
        "provenance": [source],
        "gaps": gaps,
        "absences": absences,
    }


def _evidence_touched_files(task: Mapping[str, Any]) -> list[str]:
    """File paths the Task's machine checks recorded — a section often lists no
    files of its own while the checks it ran named the exact paths, so those
    paths would otherwise be dropped from the Actions dimension."""

    paths: list[str] = []
    seen: set[str] = set()
    items = task.get("work_items") if isinstance(task.get("work_items"), list) else []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for event in item.get("evidence_events") or []:
            if not isinstance(event, Mapping):
                continue
            for candidate in event.get("files") or []:
                path = _text(candidate)
                if path and path not in seen:
                    seen.add(path)
                    paths.append(path)
    return paths


def _task_activity_window(task: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """The Task's own activity window, from its sessions — the same bound
    ``duration_seconds`` is measured over."""

    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    starts = [
        _number(s.get("first_activity_at") or s.get("started_at"))
        for s in sessions
        if isinstance(s, Mapping)
    ]
    ends = [
        _number(s.get("last_activity_at") or s.get("updated_at"))
        for s in sessions
        if isinstance(s, Mapping)
    ]
    starts = [value for value in starts if value > 0]
    ends = [value for value in ends if value > 0]
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


# Which captured tool NAME could have written which kind of ledger record. The
# names are matched by suffix so a client that prefixes its connector namespace
# (``mcp__agentacct__…``) and one that does not both resolve.
_RECORD_CALL_SUFFIXES: tuple[tuple[str, str, str], ...] = (
    ("agentacct_record_section", "record_section", "recorded section"),
    ("agentacct_record_machine_check", "record_machine_check", "recorded check"),
)


def _capture_record_shortfalls(
    task: Mapping[str, Any], name_counts: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Where the ledger holds MORE records of a kind than the capture saw calls
    that could have written them.

    Every recorded section and every recorded check cost the agent at least one
    tool call, so ``captured < recorded`` is proof the capture missed calls —
    the cheapest possible check on whether a tool-call total covers the Task.
    It compares counts only; it never guesses which calls were missed.
    """

    # Without a captured tool-NAME breakdown there is nothing to compare: a
    # session recorded before name capture shipped would otherwise read as one
    # that missed every call. No names, no claim.
    if not name_counts:
        return []
    captured: dict[str, int] = {}
    for raw_name, value in (name_counts or {}).items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        name = _text(raw_name)
        for suffix, call_label, _record_label in _RECORD_CALL_SUFFIXES:
            if name.endswith(suffix):
                captured[call_label] = captured.get(call_label, 0) + value
    recorded = {
        "record_section": len(_items(task)),
        "record_machine_check": sum(
            1 for check in _all_task_checks(task) if _check_source(check) == SOURCE_MCP
        ),
    }
    rows: list[dict[str, Any]] = []
    for _suffix, call_label, record_label in _RECORD_CALL_SUFFIXES:
        seen = captured.get(call_label, 0)
        held = recorded.get(call_label, 0)
        if held > seen:
            rows.append(
                {
                    "call_label": call_label,
                    "record_label": record_label,
                    "captured": seen,
                    "recorded": held,
                }
            )
    return rows


def _all_task_checks(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every distinct recorded check event on this Task (not just the standing
    run per identity)."""

    seen: set[tuple[str, ...]] = set()
    rows: list[Mapping[str, Any]] = []
    sources: list[Any] = [task.get("current_check_events"), task.get("task_evidence_events")]
    for item in _items(task):
        sources.extend((item.get("current_check_events"), item.get("evidence_events")))
    for source in sources:
        for event in source if isinstance(source, list) else []:
            if not isinstance(event, Mapping):
                continue
            key = evidence_event_key(event)
            if key in seen:
                continue
            seen.add(key)
            rows.append(event)
    return rows


def _actions_dimension(task: Mapping[str, Any]) -> dict[str, Any]:
    actions = _mapping(task.get("actions"))
    counts = _mapping(actions.get("tool_category_counts"))
    name_counts = _mapping(actions.get("tool_name_counts"))
    names_preview, names_elided = tool_names_preview({"tool_name_counts": name_counts})
    section_files = actions.get("touched_files") if isinstance(actions.get("touched_files"), list) else []
    # Commands an execute tool ran (hook-captured; single-line, best-effort scrubbed).
    commands = [cmd for cmd in (_text(x) for x in (actions.get("commands") or [])) if cmd]
    # Union the section-recorded files with the paths the Task's machine checks
    # named: either source alone routinely misses files the other has.
    touched: list[str] = []
    seen: set[str] = set()
    for candidate in (*section_files, *_evidence_touched_files(task)):
        path = _text(candidate)
        if path and path not in seen:
            seen.add(path)
            touched.append(path)
    provenance: list[str] = []
    gaps: list[str] = []
    absences: list[dict[str, str]] = []
    tool_calls_label = receipt_field_label("actions")
    if touched:
        provenance.append(SOURCE_MCP)
    else:
        gaps.append("No touched files were recorded for this Task's steps.")
    raw_bases = actions.get("capture_bases")
    capture_bases = [
        basis
        for basis in (raw_bases if isinstance(raw_bases, list) else [])
        if basis in (SOURCE_HOOK, SOURCE_TRANSCRIPT_SCAN)
    ]
    if counts or name_counts or commands:
        # Tool categories, names, and commands come from a client hook OR from a
        # transcript scan of the client's own store (Codex/OpenCode, whose hooks do
        # not fire) — name the ACTUAL source(s), never an assumed hook. A row
        # recorded before capture-basis tracking shipped (or a malformed non-list)
        # has no usable ``capture_bases`` and honestly falls back to hook, its
        # historical source. Gate on ``isinstance(list)`` like the projection that
        # writes it, so a corrupted scalar/string can never crash the receipt or be
        # iterated character-by-character into a silent mislabel.
        provenance.extend(capture_bases or [SOURCE_HOOK])
    elif capture_bases:
        gaps.append(f"A capture basis ran but recorded no tool calls; the {tool_calls_label} row shows none.")
        absences.append({"key": "tool_calls", "text": gaps[-1]})
    else:
        gaps.append(
            f"Tool categories were not instrumented for this session; the {tool_calls_label} row shows related paths only."
        )
        absences.append({"key": "tool_calls", "text": gaps[-1]})
    # Compute the capped preview + disclosed overflow ONCE, here, so every surface
    # (CLI, TUI, and the macOS app) renders the daemon-provided slice and never
    # re-derives the cap client-side — the single source of truth for the cap.
    preview, elided = touched_files_preview({"touched_files": touched})
    commands_shown, commands_elided = commands_preview({"commands": commands})
    # A missing stored total stays missing (never a counted 0); the synopsis
    # names each absence state and the tile the app, TUI and CLI print.
    raw_total = actions.get("tool_category_total")
    stored_total = (
        raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) else None
    )
    raw_counts = actions.get("tool_category_counts")
    source_text = ", ".join(
        dict.fromkeys(source_label(basis) for basis in (capture_bases or ([SOURCE_HOOK] if counts else [])))
    )
    # What the capture can PROVE about its own coverage: the window it ran for
    # against the Task's own activity window, and the ledger records it holds
    # against the calls capture saw. ``exact`` is earned from these, never
    # asserted — a self-consistent 60 can still describe seventeen minutes of a
    # two-day Task.
    activity_first, activity_last = _task_activity_window(task)
    coverage = {
        "captured_first_at": actions.get("captured_first_at"),
        "captured_last_at": actions.get("captured_last_at"),
        "activity_first_at": activity_first,
        "activity_last_at": activity_last,
        "record_shortfalls": _capture_record_shortfalls(task, name_counts),
    }
    synopsis = actions_synopsis(
        dict(raw_counts) if isinstance(raw_counts, Mapping) else None,
        stored_total,
        capture_known=bool(capture_bases),
        source_text=source_text,
        coverage=coverage,
    )
    if synopsis.get("state") == "partial" and synopsis.get("integrity_detail"):
        gaps.append(f"{GAP_CAPTURE_COVERAGE_PREFIX}: {synopsis['integrity_detail']}.")
    return {
        "capture_coverage": coverage,
        "tool_category_counts": dict(counts),
        "tool_category_total": stored_total,
        "actions_tile": synopsis["tile"],
        "actions_synopsis": synopsis,
        "summary_label": tool_calls_label,
        "related_paths_text": related_paths_text(len(touched)),
        "related_paths_definition": RELATED_PATHS_DEFINITION,
        "action_sources_text": source_text or None,
        "tool_name_counts": dict(name_counts),
        "tool_name_total": int(actions.get("tool_name_total") or 0),
        "tool_names_preview": names_preview,
        "tool_names_elided": names_elided,
        "touched_files": touched,
        "touched_file_count": len(touched),
        "touched_files_preview": preview,
        "touched_files_elided": elided,
        "commands": commands,
        "command_count": len(commands),
        "commands_preview": commands_shown,
        "commands_elided": commands_elided,
        "provenance": sorted(set(provenance)) or [SOURCE_NONE],
        "gaps": gaps,
        "absences": absences,
    }


def _cost_dimension(task: Mapping[str, Any]) -> dict[str, Any]:
    usage = _mapping(task.get("usage"))
    cost_complete = bool(usage.get("cost_complete"))
    estimated = usage.get("estimated_cost_usd")
    cost_basis = _text(usage.get("cost_basis")) or None
    cost_confidence = _text(usage.get("cost_confidence")) or None
    rows = int(usage.get("rows") or 0)
    state = cost_state(
        {"estimated_cost_usd": estimated, "cost_complete": cost_complete, "rows": rows}
    )
    gaps: list[str] = []
    absences: list[dict[str, str]] = []
    # One state per Task: a complete estimate is not a gap (its basis rides
    # cost_basis / cost_confidence); "incomplete" is said only when a priced
    # subtotal exists, and absence is named on its own.
    if state == "partial":
        gaps.append("Cost is incomplete: some usage rows are unpriced or excluded.")
    elif state == "unpriced":
        gaps.append("Usage was recorded for this Task, but none of it was priced.")
        absences.append({"key": "cost", "text": gaps[-1]})
    elif state == "no_usage":
        gaps.append("No usage was recorded for this Task.")
        absences.append({"key": "cost", "text": gaps[-1]})
    # The weekly-plan share has no gap SENTENCE of its own (its absence is a
    # named state on the row the record page deletes), so it joins the budget
    # from its own calibrated-or-nothing state rather than from a gap. When cost
    # itself is absent this key is subsumed and never reaches the line.
    plan_share = _mapping(task.get("plan_share")) or None
    if plan_share is not None and plan_share.get("pct") is None:
        share_text = _text(plan_share.get("sentence_text")) or plan_share_headline(plan_share)
        if share_text:
            absences.append({"key": "weekly_plan_share", "text": share_text})
    cost_fields = cost_display_fields(
        {
            "estimated_cost_usd": estimated,
            "cost_complete": cost_complete,
            "cost_basis": cost_basis,
            "cost_confidence": cost_confidence,
            "state": state,
        }
    )
    return {
        "estimated_cost_usd": estimated,
        "cost_basis": cost_basis,
        "cost_confidence": cost_confidence,
        "cost_complete": cost_complete,
        **cost_fields,
        "plan_share_headline": plan_share_headline(_mapping(task.get("plan_share")) or None),
        # The Task's share of the client's weekly plan (projection-stamped;
        # calibrated-or-nothing — pct is null with the state naming why).
        "plan_share": _plan_share_with_text(task.get("plan_share")),
        "tokens": {
            "fresh": int(usage.get("fresh_tokens") or 0),
            "cache_creation": int(usage.get("cache_creation_tokens") or 0),
            "cache_read": int(usage.get("cache_read_tokens") or 0),
            "total": int(usage.get("total_tokens") or 0),
        },
        "provenance": [SOURCE_CLIENT_LOG] if rows else [SOURCE_NONE],
        "gaps": gaps,
        "absences": absences,
    }


#: How the check rows were grouped for the record page. ``revision`` is the
#: preferred grouping; ``time_order`` is the fallback taken when it would split a
#: fail -> pass recovery across two groups.
#: Both names are spelled as MODES rather than as the axis they group on, so a
#: surface literal that happens to be the payload key ``revision`` can never be
#: mistaken for a surface deciding the grouping.
CHECK_GROUPING_BY_REVISION = "by_revision"
CHECK_GROUPING_TIME_ORDER = "time_order"


def _group_checks_by_revision(checks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """``(mode, groups)`` -- the check rows partitioned so the stamped revision
    prints ONCE per group instead of once per row, and the revision
    contradiction once per group instead of once per row.

    Grouping by revision is preferred: on task_5f7dbea9 it takes
    ``HEAD when recorded: c41d44f`` from four prints of two distinct values down
    to two. It is taken ONLY when no supersession pair would be split across two
    groups, because ``WorkRecordChecks``' own contract is that a fail -> pass
    recovery reads as two ADJACENT rows -- a grouping that files the failure
    under one commit and its fix under another destroys exactly the story the
    rows exist to tell. When a pair would be split, the fallback is strict time
    order with the label hoisted only over RUNS of adjacent rows sharing a value.

    That choice is made HERE, in the reducer, and shipped as
    ``evidence.revision_groups``: a surface that guessed it would be a second
    grouping vocabulary, and two surfaces guessing differently would group the
    same record two ways.

    ``groups`` fully determines both the grouping and the row order: walking the
    groups and their ``event_ids`` in order yields every row exactly once.
    """

    if not checks:
        return CHECK_GROUPING_BY_REVISION, []

    def commit_of(check: Mapping[str, Any]) -> str:
        return _text((check.get("revision") or {}).get("commit"))

    # Rows in strict time order first. The payload's own row order is per check
    # IDENTITY (every run of one check together), which reads as alphabetical
    # noise on a page; grouping settles the render order, so it settles it
    # temporally in BOTH modes.
    in_time_order = [
        check for _, check in sorted(enumerate(checks), key=lambda pair: (_number(pair[1].get("at")), pair[0]))
    ]

    # Candidate grouping: distinct stamped commit, each group in time order and
    # the groups themselves ordered by when their first row happened. A row with
    # no stamp joins the single unstamped group -- "not captured" is one state,
    # not one state per row.
    order: list[str] = []
    partition: dict[str, list[dict[str, Any]]] = {}
    for check in in_time_order:
        key = commit_of(check)
        if key not in partition:
            order.append(key)
            partition[key] = []
        partition[key].append(check)

    # Would that split a recovery? Both directions of the supersession link are
    # on the row, so the test is exact rather than heuristic.
    by_event = {_text(check.get("event_id")): check for check in checks if _text(check.get("event_id"))}
    splits = False
    for check in checks:
        for field in ("superseded_by_event_id", "supersedes_check_event_id"):
            other = by_event.get(_text(check.get(field)))
            if other is not None and commit_of(other) != commit_of(check):
                splits = True
                break
        if splits:
            break

    if splits:
        mode = CHECK_GROUPING_TIME_ORDER
        runs: list[list[dict[str, Any]]] = []
        for check in in_time_order:
            if runs and commit_of(runs[-1][0]) == commit_of(check):
                runs[-1].append(check)
            else:
                runs.append([check])
        grouped = runs
    else:
        mode = CHECK_GROUPING_BY_REVISION
        grouped = [partition[key] for key in order]

    groups: list[dict[str, Any]] = []
    for index, rows in enumerate(grouped):
        head = rows[0]
        # The banner is earned only by a DUPLICATE: one row's own sentence is
        # already stated once, and lifting it off a single row buys no ink while
        # costing the row the sentence.
        texts = [_text(row.get("revision_contradiction_text")) for row in rows]
        shared = (
            texts[0]
            if len(rows) > 1 and texts[0] and all(text == texts[0] for text in texts)
            else None
        )
        for row in rows:
            row["revision_group_index"] = index
            row["revision_label_hoisted"] = True
            if shared:
                row["revision_contradiction_text"] = None
        groups.append(
            {
                "revision": head.get("revision"),
                # The group header, verbatim from the row vocabulary -- never a
                # second wording of the same stamp.
                "label": _text(head.get("revision_label")) or REVISION_NOT_CAPTURED,
                "event_ids": [_text(row.get("event_id")) for row in rows],
                "row_count": len(rows),
                # 0 or 1 banner. None when the rows disagree: a merged sentence
                # covering two different contradictions would be a second
                # vocabulary, so each row keeps its own instead.
                "contradiction_text": shared,
            }
        )
    return mode, groups


def _hoist_check_meta(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Lift the check facts that are IDENTICAL on every row off the rows and onto
    the section heading, then rewrite each row's ``meta_line`` without them.

    On task_5f7dbea9 ``test`` and ``Agent-reported`` are the same on all four
    rows -- six prints of two facts -- and hoisting them leaves
    ``Failed · Exit 1`` / ``Passed · Exit 0``. Only the type and the source are
    ever hoisted: the result and the exit code are what DISTINGUISHES one row
    from another, so a record whose rows all passed still states it per row.
    """

    def uniform(field: str) -> str | None:
        values = {_text(check.get(field)) for check in checks}
        if len(values) != 1:
            return None
        only = values.pop()
        return only or None

    evidence_type = uniform("evidence_type") if checks else None
    source = uniform("source_label") if checks else None
    for check in checks:
        check["meta_line"] = check_meta_line(
            check.get("result_label"),
            check.get("exit_code"),
            None if evidence_type else (_text(check.get("evidence_type")) or None),
            None if source else (_text(check.get("source_label")) or None),
        )
    return {"evidence_type": evidence_type, "source_label": source}


def _evidence_dimension(checks: list[Mapping[str, Any]], strength: Mapping[str, Any]) -> dict[str, Any]:
    sources = sorted({_text(check.get("source")) for check in checks if _text(check.get("source"))})
    gaps: list[str] = []
    if not checks:
        gaps.append("No machine checks were recorded for this Task.")
    # Only CHECKABLE steps can owe a check: research/docs steps are declared
    # non-verifiable by the coverage ledger, so demanding evidence for them
    # would inflate the gap count with impossible asks.
    unchecked_steps = int((strength.get("by_tier") or {}).get("unchecked") or 0)
    if unchecked_steps:
        have = "has" if unchecked_steps == 1 else "have"
        gaps.append(f"{count_noun(unchecked_steps, 'completed step')} {have} no linked passing check.")
    not_run = int(strength.get("checks_not_run") or 0)
    if not_run:
        gaps.append(f"{count_noun(not_run, 'check')} {CHECK_NOT_RUN_WORDS}, so {'it proves' if not_run == 1 else 'they prove'} nothing.")
    # The rows are mutated in place by both passes below, so shape them once
    # here rather than handing two passes two different copies.
    rows = [dict(check) for check in checks]
    hoisted = _hoist_check_meta(rows)
    grouping_mode, revision_groups = _group_checks_by_revision(rows)
    return {
        "checks": rows,
        # The evidence section's ONE heading line: the tally, the tier stated
        # here and nowhere else on the page, then whichever of the source and
        # the type was uniform enough to lift off every row.
        "heading_line": checks_heading_line(
            strength.get("check_tally_text"),
            TIER_LABELS.get(_text(strength.get("strongest_tier"))) if strength.get("gradeable") else None,
            hoisted["source_label"],
            hoisted["evidence_type"],
        )
        or None,
        # What the heading line took OFF the rows, so a surface can tell a
        # hoisted absence from a fact that was never recorded.
        "hoisted_source_label": hoisted["source_label"],
        "hoisted_evidence_type": hoisted["evidence_type"],
        "revision_grouping_mode": grouping_mode,
        "revision_groups": revision_groups,
        "checks_total": int(strength.get("checks_total") or 0),
        "checks_passed": int(strength.get("checks_passed") or 0),
        "checks_failed": int(strength.get("checks_failed") or 0),
        "checks_not_run": int(strength.get("checks_not_run") or 0),
        "checks_superseded": int(strength.get("checks_superseded") or 0),
        "checks_earlier_failed": int(strength.get("checks_earlier_failed") or 0),
        "checks_tile": strength.get("checks_tile"),
        "check_tally_text": strength.get("check_tally_text"),
        "check_runs_state": strength.get("check_runs_state"),
        "provenance": sources or [SOURCE_NONE],
        "gaps": gaps,
        # This dimension's budgeted absences all arrive as REVIEWER gaps (no
        # commit recorded, unordered file operations), which are appended after
        # the dimensions are built; see ``_ABSENCE_KEY_BY_GAP_CODE``.
        "absences": [],
    }


def _outcome_dimension(
    decision: Mapping[str, Any],
    verification: Mapping[str, Any],
    decision_brief: Mapping[str, Any],
    checks: list[Mapping[str, Any]],
    canonical: Mapping[str, Any] | None = None,
    task: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    asserted_by = _text(decision.get("asserted_by")) or "none"
    items = _items(task) if isinstance(task, Mapping) else []
    gaps: list[str] = []
    # ``inactive`` joins the non-terminal set: agentacct inferred the Task went
    # quiet, but nothing finished — so it still owes a terminal outcome and reads
    # as a gap, never a settled result.
    if decision.get("key") in {"in_progress", "observed", "unknown", "inactive"}:
        gaps.append("No terminal outcome has been recorded for this Task yet.")
    dimension: dict[str, Any] = {
        "decision_status": decision.get("key"),
        "statement": decision.get("statement"),
        "asserted_by": asserted_by,
        "per_step": {
            "verified": int(verification.get("verified_step_count") or 0),
            "total": int(verification.get("total_step_count") or 0),
            "agent_reported": int(verification.get("agent_reported_step_count") or 0),
        },
        "asserted_by_label": asserted_by_label(asserted_by),
        "asserted_by_phrase": asserted_by_phrase(asserted_by),
        "owner": decision_brief.get("owner"),
        "next_action": decision_brief.get("next_action"),
        # The agent's own outcome words: the newest step summary, verbatim,
        # labeled as the agent's report (never a verified statement).
        **_outcome_summary(items),
        # The agent's recorded continuation point, withheld once the decision
        # reports the work done.
        **_outcome_next_step(items, _text(decision.get("key"))),
        "provenance": [_asserted_by_source(asserted_by, checks)],
        "gaps": gaps,
        # No terminal outcome is a MISSING DECISION, not an uncaptured
        # measurement: it belongs in the decision axis the page leads with, never
        # collapsed into the tail's not-captured line.
        "absences": [],
    }
    # Detail-line facts (#3), surfaced ONLY when the Task went quiet
    # (inactive / mostly_done): ``quiet_since`` = this Task's newest event (when it
    # fell silent), ``newer_session_started_at`` = the newer session's start the
    # went-quiet predicate keyed off. Null on every other outcome. These are
    # factual timestamps, never a completion claim — the app renders a "quiet since
    # …" sub-line from them, nothing more.
    source = canonical if isinstance(canonical, Mapping) else {}
    if decision.get("key") in {"inactive", "mostly_done"}:
        dimension["quiet_since"] = source.get("quiet_since")
        dimension["newer_session_started_at"] = source.get("newer_session_started_at")
    else:
        dimension["quiet_since"] = None
        dimension["newer_session_started_at"] = None
    return dimension


def _item_time(item: Mapping[str, Any]) -> float:
    return _number(item.get("updated_at") or item.get("started_at"))


_OUTCOME_DONE_KEYS = frozenset({"reported", "verified"})


def _outcome_summary(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    with_summary = [item for item in items if _text(item.get("summary"))]
    if not with_summary:
        return {"summary": None, "summary_label": None, "summary_section_title": None}
    newest = max(with_summary, key=_item_time)
    return {
        "summary": _text(newest.get("summary")),
        "summary_label": source_label(SOURCE_MCP),
        "summary_section_title": _text(newest.get("title") or newest.get("objective")) or None,
    }


def _outcome_next_step(items: list[Mapping[str, Any]], decision_key: str) -> dict[str, Any]:
    with_next = [item for item in items if _text(item.get("next_step"))]
    if not with_next or decision_key in _OUTCOME_DONE_KEYS:
        return {"next_step": None, "next_step_section_title": None}
    newest = max(with_next, key=_item_time)
    return {
        "next_step": _text(newest.get("next_step")),
        "next_step_section_title": _text(newest.get("title") or newest.get("objective")) or None,
    }


# --- Roll-ups (dimensions 7 & 8) ---------------------------------------------

#: Reviewer gaps that are ALSO a named absence of something the record could
#: have captured, and therefore belong on the one absence line. Keyed by gap
#: CODE, never by sentence: a code is a typed fact the reducer already emits,
#: while matching on a sentence would put the vocabulary in two files and let
#: them drift.
#:
#: The reviewer gaps NOT listed here stay full sentences and are deliberately
#: outside the budget -- ``no_change_description``, ``subagents_recorded_no_work``
#: and ``declared_paths_unobserved`` each name something the record HAS and
#: contradicts, not something it failed to measure, and a noun could not carry
#: them.
_ABSENCE_KEY_BY_GAP_CODE: dict[str, str] = {
    GAP_CODE_NO_COMMIT: "revision",
    GAP_CODE_FILE_OPERATIONS_UNORDERED: "tool_call_order",
}


def _absence_budget(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    """The record's ONE collapsed absence statement, plus the detail behind it.

    Absence stays a NAMED state -- every sentence survives, in ``detail`` -- but
    it collapses to one line. Measured on task_5f7dbea9 the tail was eight
    statements of which six were "we do not know"; those six are now four nouns
    on one line with a disclosure.

    Composed HERE so no surface decides which absences are "the same one": two
    surfaces collapsing the set differently would be two vocabularies.
    ``line`` is None on an empty budget and the surface prints NOTHING -- a
    record knows what it failed to capture, never that it captured everything.
    """

    detail: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = _text(item.get("absence_key"))
        if not key or key not in NOT_CAPTURED_NOUNS or key in seen:
            continue
        seen.add(key)
        detail.append(
            {
                "key": key,
                "noun": NOT_CAPTURED_NOUNS[key],
                # The full sentence stays the gap's own words, so the disclosure
                # and the gap list can never say different things.
                "text": _text(item.get("reason")),
            }
        )
    keys = collapse_not_captured_keys(list(seen))
    # Declaration order for the detail too, so the disclosure reads in the same
    # order as the line it opens.
    order = list(NOT_CAPTURED_NOUNS)
    detail.sort(key=lambda row: order.index(row["key"]))
    return {
        "line": not_captured_line(keys),
        "keys": keys,
        "detail": detail,
        "detail_count": len(detail),
    }


def _roll_up_gaps(
    dimensions: Mapping[str, Mapping[str, Any]],
    coverage: list[Mapping[str, Any]],
    task: Mapping[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    # Budgeted absences that are NOT gaps of their own; see the loop below.
    budget_only: list[dict[str, Any]] = []

    def add(
        dimension: str,
        reason: str,
        kind: str | None = None,
        code: str | None = None,
        absence_key: str | None = None,
    ) -> None:
        capture_shortfall = str(reason).startswith(GAP_CAPTURE_COVERAGE_PREFIX)
        resolved = kind or (
            GAP_KIND_BOOKKEEPING
            if dimension in _BOOKKEEPING_GAP_DIMENSIONS and not capture_shortfall
            else GAP_KIND_BLOCKS_REVIEW
        )
        resolved_code = code or (GAP_CODE_CAPTURE_COVERAGE if capture_shortfall else GAP_CODE_DIMENSION)
        items.append(
            {
                "dimension": dimension,
                "dimension_label": receipt_field_label(dimension),
                "reason": reason,
                # HOW MUCH the gap prevents, beside the kind that says whether
                # it prevents anything. Without it the order was "whichever
                # reducer appended first", which put an unordered file list
                # above three sessions that burned 2.4M tokens and recorded
                # nothing.
                "code": resolved_code,
                "rank": gap_rank(resolved_code),
                # WHAT THE GAP PREVENTS, not which ingestion source was silent.
                # A reviewer who cannot locate the commit is stuck; a missing
                # model name is agentacct's own bookkeeping. Ranking by source
                # put four bookkeeping lines at the top of every receipt and the
                # reviewer-facing ones nowhere.
                "kind": resolved,
                "kind_label": gap_kind_label(resolved),
                # The noun this gap contributes to the ONE absence line, or None
                # when the gap is a full sentence no noun could carry. The gap
                # keeps its own sentence either way: the budget changes where
                # absence is PRINTED, never whether it is named.
                "absence_key": absence_key,
            }
        )

    for name, dimension in dimensions.items():
        # Each dimension declares which of its gap sentences is a budgeted
        # absence, at the site that appends the sentence, so the pairing is by
        # construction rather than by matching text after the fact.
        keyed = {
            _text(entry.get("text")): _text(entry.get("key"))
            for entry in (dimension.get("absences") or [])
            if isinstance(entry, Mapping) and _text(entry.get("text"))
        }
        for reason in dimension.get("gaps", []) or []:
            add(name, reason, absence_key=keyed.pop(_text(reason), None) or None)
        # An absence with no gap sentence of its own (the weekly plan share)
        # still joins the budget, carrying its own named state as the detail. It
        # is deliberately NOT appended to ``items``: it is already a named state
        # on its own row, and listing it as a gap as well is exactly the
        # duplication the budget exists to remove.
        for text, key in keyed.items():
            budget_only.append({"absence_key": key, "reason": text})
    # The coverage table (recorded / unavailable / not_recorded per dimension)
    # already ships as its own ``coverage`` block. Folding every non-recorded
    # row into gaps duplicated the per-dimension gaps and drowned the real,
    # per-task ones in structural facts (no org control plane, no artifact
    # store) that are true for every single-machine task — so gaps stay
    # per-dimension only. ``coverage`` is unused here now but kept in the
    # signature so callers do not change.
    _ = coverage
    unlinked = int(task.get("session_unlinked_work_count") or 0)
    if unlinked:
        add(
            "actors",
            f"{count_noun(unlinked, 'work item')} could not be tied to an exact session.",
            code=GAP_CODE_WORK_NOT_TIED_TO_SESSION,
        )
    for dimension, reason, code in _reviewer_gaps(dimensions, task):
        add(
            dimension,
            reason,
            kind=GAP_KIND_BLOCKS_REVIEW,
            code=code,
            absence_key=_ABSENCE_KEY_BY_GAP_CODE.get(code),
        )
    # Reviewer-facing first, provenance bookkeeping last, and inside each kind
    # by what the gap prevents; insertion order holds inside a rank (``sort``
    # is stable), so two gaps of equal weight stay in reducer order.
    items.sort(key=lambda item: (0 if item["kind"] == GAP_KIND_BLOCKS_REVIEW else 1, item["rank"]))
    return {
        "items": items,
        # The ONE collapsed absence statement plus its disclosure. Built from the
        # very gap items above, so the line and the list cannot disagree.
        "not_captured": _absence_budget([*items, *budget_only]),
        "count": len(items),
        "blocks_review_count": sum(1 for item in items if item["kind"] == GAP_KIND_BLOCKS_REVIEW),
        "bookkeeping_count": sum(1 for item in items if item["kind"] == GAP_KIND_BOOKKEEPING),
    }


# Dimensions whose gaps are agentacct's own provenance bookkeeping rather than
# something that stops a reviewer from checking the work.
_BOOKKEEPING_GAP_DIMENSIONS = frozenset({"actors", "actions", "cost"})


def _reviewer_gaps(
    dimensions: Mapping[str, Mapping[str, Any]], task: Mapping[str, Any]
) -> list[tuple[str, str, str]]:
    """Gaps named by WHAT A REVIEWER CANNOT DO, derived from data the reducer
    already holds.

    Every one of these is a question a reviewer asks first and the receipt could
    already answer: where is this in the repo, what changed, in what order, what
    did the subagents do, and were the declared paths ever observed.
    """

    found: list[tuple[str, str, str]] = []
    evidence = dimensions.get("evidence") if isinstance(dimensions.get("evidence"), Mapping) else {}
    actions = dimensions.get("actions") if isinstance(dimensions.get("actions"), Mapping) else {}
    outcome = dimensions.get("outcome") if isinstance(dimensions.get("outcome"), Mapping) else {}
    checks = evidence.get("checks") if isinstance(evidence.get("checks"), list) else []
    checks = [check for check in checks if isinstance(check, Mapping)]

    # 1. Where is this work in the repository?
    if checks and not any(_text((check.get("revision") or {}).get("commit")) for check in checks):
        found.append(("evidence", GAP_NO_COMMIT_RECORDED, GAP_CODE_NO_COMMIT))

    # 2. What actually changed?
    described = any(_text(check.get("summary")) for check in checks) or _text(outcome.get("summary"))
    if not described:
        found.append(("outcome", GAP_NO_CHANGE_DESCRIPTION, GAP_CODE_NO_CHANGE_DESCRIPTION))

    # 3. In what order did the edits happen?
    if int(actions.get("touched_file_count") or 0) > 0:
        found.append(("actions", GAP_FILE_OPERATIONS_UNORDERED, GAP_CODE_FILE_OPERATIONS_UNORDERED))

    # 4. What did the supporting sessions do for their tokens?
    silent_sessions, silent_tokens = unrecorded_subagent_sessions(task)
    silent = gap_subagents_recorded_no_work(silent_sessions, silent_tokens)
    if silent:
        found.append(("actors", silent, GAP_CODE_SUBAGENTS_SILENT))

    # 5. Were the paths the checks named ever observed being written?
    declared = {path for check in checks for path in (check.get("files") or []) if _text(path)}
    observed_edits = int(_mapping(actions.get("tool_category_counts")).get("edit") or 0)
    if declared and observed_edits == 0 and _text(actions.get("action_sources_text")):
        unobserved = gap_declared_paths_unobserved(len(declared))
        if unobserved:
            found.append(("evidence", unobserved, GAP_CODE_DECLARED_PATHS_UNOBSERVED))
    return found


def _roll_up_provenance(dimensions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_field: dict[str, list[str]] = {}
    present: set[str] = set()
    for name, dimension in dimensions.items():
        sources = [source for source in (dimension.get("provenance") or []) if source]
        by_field[name] = sources or [SOURCE_NONE]
        # ``none`` is an absence, not a source: it stays per dimension (and in
        # the legend a per-dimension cell needs) but never joins the sources
        # that contributed to this record.
        present.update(source for source in by_field[name] if source != SOURCE_NONE)
    named = sorted({source for sources in by_field.values() for source in sources})
    return {
        "by_dimension": by_field,
        "sources_present": sorted(present),
        # Every label a per-dimension cell prints, ``No source recorded`` included.
        "legend": {source: PROVENANCE_LEGEND[source] for source in named if source in PROVENANCE_LEGEND},
        # The named absence a surface prints when no dimension has a source.
        "sources_absent_text": None if present else source_label(SOURCE_NONE),
        # Each present source with its display label, legend sentence and tier.
        "sources": [source_entry(source) for source in sorted(present)],
    }


def source_entry(source: Any) -> dict[str, Any]:
    """``{key, label, legend, tier_key, tier_label}`` for one provenance source."""

    key = _text(source) or SOURCE_NONE
    entry = SOURCE_LABELS.get(key)
    if entry is None:
        return {"key": key, "label": source_label(key), "legend": None, "tier_key": None, "tier_label": None}
    return {"key": key, **{name: entry.get(name) for name in ("label", "legend", "tier_key", "tier_label")}}


# --- Public entry point -------------------------------------------------------

def _sessions_block(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Task's constituent sessions, grouped root -> its members, with
    primary/continuation and root/subagent roles.

    A pure reshape of ``task["root_groups"]`` and ``task["sessions"]`` (no new
    data) into the drill-down index the Work surface expands: each member ref is
    a ``{client, client_session_id}`` the app resolves against the existing
    ``/v1/session`` endpoint for that session's steps/checks/descendants.
    """

    primary = _mapping(task.get("primary_root"))
    primary_key = (_text(primary.get("client")), _text(primary.get("client_session_id")))
    from .task_timeline import session_display_titles

    titles = session_display_titles(task)
    sessions_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    raw_sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    for session in raw_sessions:
        if not isinstance(session, Mapping):
            continue
        key = (_text(session.get("client")), _text(session.get("client_session_id")))
        if key[0] and key[1]:
            sessions_by_key[key] = session

    groups: list[dict[str, Any]] = []
    raw_groups = task.get("root_groups") if isinstance(task.get("root_groups"), list) else []
    for group in raw_groups:
        if not isinstance(group, Mapping):
            continue
        root = _mapping(group.get("root"))
        root_key = (_text(root.get("client")), _text(root.get("client_session_id")))
        members: list[dict[str, Any]] = []
        member_refs = group.get("session_keys") if isinstance(group.get("session_keys"), list) else []
        for ref in member_refs:
            if not isinstance(ref, Mapping):
                continue
            key = (_text(ref.get("client")), _text(ref.get("client_session_id")))
            if not key[0] or not key[1]:
                continue
            session = sessions_by_key.get(key, {})
            members.append(
                {
                    "client": key[0],
                    "client_session_id": key[1],
                    "session_kind": _text(session.get("session_kind")) or None,
                    "role": "root" if key == root_key else "subagent",
                    "title": titles.get(key) or None,
                    "project": _text(session.get("project")) or None,
                    "last_activity_at": _number(session.get("last_activity_at")) or None,
                }
            )
        groups.append(
            {
                "root": {"client": root_key[0], "client_session_id": root_key[1]},
                "role": "primary" if root_key == primary_key else "continuation",
                "lineage_state": _text(group.get("lineage_state")) or None,
                "supporting_count": int(group.get("supporting_count") or 0),
                "members": members,
            }
        )
    return groups


def build_receipt(
    task: Mapping[str, Any],
    *,
    public_task_id: str,
    title: str,
    control: Mapping[str, Any] | None = None,
    timeline_limit: int = 50,
    latest_store_activity_at: float | None = None,
    session_starts: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Project one enriched Task into a canonical ``agentacct.receipt.v1``.

    ``task`` is a single element of a decorated, evidence-attached Task
    projection (``build_store_task_projection(...)["tasks"][i]`` after
    ``_attach_evidence_to_task_projection``). ``build_task_intelligence`` is
    reused for the states / decision brief / verification / findings / coverage
    / lanes / timeline; this function adds the two orthogonal axes and the
    unified provenance and gaps roll-ups.
    """

    intelligence = build_task_intelligence(
        task,
        public_task_id=public_task_id,
        title=title,
        control=control,
        timeline_limit=timeline_limit,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
    )
    verification = step_verification_counts(task)
    checks = _project_checks(task)
    evidence_strength = _evidence_strength(task, checks, verification)
    canonical = reduce_task_outcome(
        task,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
    )
    decision = _decision_status(
        task,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
        canonical=canonical,
    )
    handoff = _handoff_marker(canonical)
    decision_brief = _mapping(intelligence.get("decision_brief"))
    classified = _attention_block(task, canonical, decision, task_title=title)
    attention = classified[1] if classified is not None else None

    dimensions: dict[str, dict[str, Any]] = {
        "task": _task_dimension(task, title),
        "actors": _actors_dimension(task, intelligence),
        "actions": _actions_dimension(task),
        "cost": _cost_dimension(task),
        "evidence": _evidence_dimension(checks, evidence_strength),
        "outcome": _outcome_dimension(
            decision, verification, decision_brief, checks, canonical, task
        ),
    }
    coverage = intelligence.get("coverage") if isinstance(intelligence.get("coverage"), list) else []
    # Roll both meta-dimensions up over ONLY the six content dimensions, before
    # inserting them — ``gaps`` and ``provenance`` carry no provenance of their
    # own, so including them would manufacture a spurious ``none`` source.
    gaps = _roll_up_gaps(dimensions, coverage, task)
    provenance = _roll_up_provenance(dimensions)
    dimensions["gaps"] = gaps
    dimensions["provenance"] = provenance

    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "task_id": public_task_id,
        "title": title,
        # The one honest leading line — what changed + how well proven — assembled
        # once here from the axes already computed, so every surface leads with
        # identical words. A roll-up, so it carries no provenance of its own and
        # stays out of the provenance/gaps folds below.
        "verdict": {
            "headline": verdict_headline(decision, evidence_strength),
            "proof_clause": verdict_proof_clause(evidence_strength),
            "decision_key": decision.get("key"),
            "decision_label": decision.get("label"),
            "evidence_key": evidence_strength.get("key"),
            "asserted_by": decision.get("asserted_by"),
            "asserted_by_label": decision.get("asserted_by_label"),
            # Legacy joined gap (evidence then cost); new surfaces print the
            # typed parts: ``gap_label`` + ``gap_text`` (evidence only), with
            # cost absence on the Cost row (``dimensions.cost.gap_text``).
            "gap": verdict_gap_line(evidence_strength, dimensions["cost"]),
            **verdict_gap(evidence_strength, dimensions["cost"]),
            "health_window": verdict_health_window(task, evidence_strength, canonical),
        },
        # The ONE attention block for this Task (None when nothing needs you):
        # the same dict each summary row and /v1/attention carry.
        "attention": attention,
        "attention_open": bool(attention and attention.get("open")),
        # One header per receipt field, shared by CLI, Markdown and app.
        "field_labels": dict(RECEIPT_FIELD_LABELS),
        # The filter group this Task belongs to (a filter only; rows keep their
        # own decision word).
        "group_key": decision_group(decision.get("key"), bool(attention and attention.get("open"))),
        # The handoff marker's words, present only when it adds to the decision.
        "lifecycle_marker_text": (
            LIFECYCLE_MARKER_TEXT if handoff.get("handed_off") and decision.get("key") != "handed_off" else None
        ),
        "axes": {
            "decision_status": decision,
            "evidence_strength": evidence_strength,
            # A third, orthogonal signal: the deliberate-stop lifecycle marker.
            # Kept out of ``decision_status`` on purpose so a handoff can be shown
            # BESIDE a finding/blocked headline instead of being masked by it.
            "handoff": handoff,
            "orthogonality_note": (
                "Evidence coverage and decision status are separate axes: an agent reporting "
                "'done' never adds a passing check, and a human review or approval never "
                "counts as machine verification."
            ),
        },
        "dimensions": dimensions,
        # The Task's constituent sessions, grouped root -> members, so the Work
        # surface can nest each session's /v1/session drill-down under the
        # Receipt. Additive; existing consumers ignore it.
        "sessions": _sessions_block(task),
        # Rich sub-objects reused verbatim by the CLI / app / TUI surfaces.
        "lanes": intelligence.get("lanes"),
        "timeline": intelligence.get("timeline"),
        "findings": intelligence.get("findings"),
        "coverage": intelligence.get("coverage"),
        "duration_seconds": intelligence.get("duration_seconds"),
        "models": intelligence.get("models"),
        "usage": intelligence.get("usage"),
        "raw_evidence": intelligence.get("raw_evidence"),
    }


def build_receipt_summary(
    task: Mapping[str, Any],
    *,
    public_task_id: str,
    title: str,
    latest_store_activity_at: float | None = None,
    session_starts: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """A compact Receipt row for a task LIST — the two axes plus cost/activity.

    Shares the exact decision/evidence reducers with ``build_receipt`` so the
    list and the detail can never disagree, without paying for the full 8
    dimensions per row.
    """

    verification = step_verification_counts(task)
    checks = _project_checks(task)
    evidence_strength = _evidence_strength(task, checks, verification)
    canonical = reduce_task_outcome(
        task,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
    )
    decision = _decision_status(
        task,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
        canonical=canonical,
    )
    usage = _mapping(task.get("usage"))
    summary_cost = {
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        "cost_complete": bool(usage.get("cost_complete")),
        "cost_basis": _text(usage.get("cost_basis")) or None,
        "cost_confidence": _text(usage.get("cost_confidence")) or None,
        "rows": int(usage.get("rows") or 0),
    }
    classified = _attention_block(task, canonical, decision, task_title=title)
    attention = classified[1] if classified is not None else None
    attention_open = bool(attention and attention.get("open"))
    handed_off = bool(canonical.get("handoff_current"))
    return {
        "task_id": public_task_id,
        "title": title,
        # The same one-line verdict the full Receipt leads with (headline + gap
        # only — the list row stays one line, so no health window). Same
        # reducers, so a row and its detail can never word the verdict
        # differently.
        "verdict": {
            "headline": verdict_headline(decision, evidence_strength),
            "proof_clause": verdict_proof_clause(evidence_strength),
            "decision_key": decision["key"],
            "evidence_key": evidence_strength["key"],
            "gap": verdict_gap_line(evidence_strength, summary_cost),
            **verdict_gap(evidence_strength, summary_cost),
        },
        "decision_status": {
            "key": decision["key"],
            "label": decision["label"],
            "asserted_by": decision["asserted_by"],
            "asserted_by_label": decision["asserted_by_label"],
            "asserted_by_phrase": decision["asserted_by_phrase"],
            # The one-line explanation + (for blocked/failed) the newest
            # blocker's own words, so list rows can say WHY without a click.
            "statement": decision["statement"],
            "blocker": decision.get("blocker"),
        },
        # The recency-aware handoff lifecycle marker for the list row's parallel
        # chip. A flat bool keeps the row compact; the detail Receipt carries the
        # full ``axes.handoff`` object. Rendered as a chip only when it is not
        # already the decision word (see ``_handoff_marker``).
        "handed_off": handed_off,
        "lifecycle_marker_text": (
            LIFECYCLE_MARKER_TEXT if handed_off and decision["key"] != "handed_off" else None
        ),
        "evidence_strength": {
            "key": evidence_strength["key"],
            "gradeable": evidence_strength["gradeable"],
            "strongest_tier": evidence_strength["strongest_tier"],
            "checkable_total": evidence_strength["checkable_total"],
            "checked_total": evidence_strength["checked_total"],
            "by_tier": evidence_strength["by_tier"],
            # Check tallies for the list's checks column — same reducer values
            # the full Receipt serves, so list and detail can never disagree.
            "checks_total": evidence_strength["checks_total"],
            "checks_passed": evidence_strength["checks_passed"],
            "checks_failed": evidence_strength["checks_failed"],
            "hidden_in_subagents": evidence_strength["hidden_in_subagents"],
            "unattributed_checks": evidence_strength["unattributed_checks"],
            "checks_superseded": evidence_strength["checks_superseded"],
            "checks_earlier_failed": evidence_strength["checks_earlier_failed"],
            "coverage_hero": evidence_strength["coverage_hero"],
            "coverage_row": evidence_strength["coverage_row"],
            "coverage_tile": evidence_strength["coverage_tile"],
            "checks_tile": evidence_strength["checks_tile"],
            "check_tally_text": evidence_strength["check_tally_text"],
            "check_runs_state": evidence_strength["check_runs_state"],
            "checks_not_run": evidence_strength["checks_not_run"],
            "checks_not_run_text": evidence_strength["checks_not_run_text"],
        },
        # The ONE attention block (None when nothing needs you) and its open
        # predicate — the only "still needs you" signal a row carries.
        "attention": attention,
        "attention_open": attention_open,
        # The reducer's attention order class for an open item (0 failed checks
        # and failed steps, 1 blockers, 2 checks that could not run); None when
        # nothing is open. Every surface sorts by (attention_order, recency).
        "attention_order": classified[0] if classified is not None and attention_open else None,
        "group_key": decision_group(decision["key"], attention_open),
        "cost": {
            "estimated_cost_usd": usage.get("estimated_cost_usd"),
            "cost_basis": summary_cost["cost_basis"],
            "cost_confidence": summary_cost["cost_confidence"],
            "cost_complete": bool(usage.get("cost_complete")),
            **cost_display_fields(summary_cost),
            # The weekly-plan share for list rows (same projection stamp the
            # detail receipt carries — the two can never disagree).
            "plan_share": _plan_share_with_text(task.get("plan_share")),
        },
        "session_count": int(task.get("session_count") or 0),
        # The primary root's {client, client_session_id} — the one id a list row
        # carries, so the Work surface can deep-link a session to its Task.
        "primary_root": _mapping(task.get("primary_root")) or None,
        # Friendly project label from the same boundary reducer as the full
        # Receipt. This is additive for /v1/tasks and lets a bounded attention
        # row retain enough context without fetching one full Receipt per row.
        "project": _boundary(task)["project"],
        "last_activity_at": _number(task.get("last_activity_at")) or None,
    }


def build_attention_reason(
    task: Mapping[str, Any],
    *,
    latest_store_activity_at: float | None = None,
    session_starts: Mapping[str, float] | None = None,
    task_title: Any = None,
) -> tuple[int, dict[str, Any]] | None:
    """Return the operational attention class and its truthful leading reason.

    The integer is an internal ordering class, not a business-priority score:
    standing machine findings and recorded failed steps lead unresolved
    blockers, matching the existing dashboard grouping. ``None`` means the
    Task does not currently need review. Human-resolved and superseded findings
    are excluded by the canonical decision reducer rather than by check totals,
    so historical failures cannot leak back into the queue.

    The reason is the ONE attention block every surface renders (see
    :func:`_attention_block`): the same dict rides the receipt, each summary
    row and ``/v1/attention``.
    """

    canonical = reduce_task_outcome(
        task,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
    )
    decision = _decision_status(
        task,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
        canonical=canonical,
    )
    return _attention_block(task, canonical, decision, task_title=task_title)


def _check_observed_at(check: Mapping[str, Any]) -> float:
    return _number(check.get("created_at") or check.get("occurred_at") or check.get("time"))


def _linked_step(task: Mapping[str, Any], check: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The one work step a check attaches to by ``section_id`` / ``work_id``."""

    wanted = {value for value in (_text(check.get("section_id")), _text(check.get("work_id"))) if value}
    if not wanted:
        return None
    matches = [
        item
        for item in _items(task)
        if _text(item.get("section_id")) in wanted or _text(item.get("work_id")) in wanted
    ]
    return matches[0] if len(matches) == 1 else None


def attention_label(
    kind: str,
    *,
    evidence_type: Any = None,
    result: Any = None,
    check_name: Any = None,
    exit_code: Any = None,
    section_title: Any = None,
    task_title: Any = None,
) -> str:
    """The one-line proof label, never repeating the reason noun that rides
    beside it (``reason_label``): ``Failed build check · <name> · exit 0`` for a
    failed check (result verb + check kind are its identity), ``Typecheck check
    · <name> · exit 1`` for a check that could not run, and the step title for a
    blocker or failed step. A step title equal to the Task title is dropped (the
    surface already shows it), leaving an empty label."""

    if kind in {"failed_check", "check_not_run"}:
        kind_word = _text(evidence_type)
        named_kind = bool(kind_word) and kind_word not in {"other", "check"}
        if kind == "failed_check":
            head = f"Failed {kind_word} check" if named_kind else "Failed check"
        else:
            # The reason label ("Check could not run") always rides beside this
            # label, so the identity line names the check without repeating it.
            head = f"{kind_word[:1].upper()}{kind_word[1:]} check" if named_kind else "Check"
        parts = [head]
        if _text(check_name):
            parts.append(_text(check_name))
        code = _int_or_none(exit_code)
        if code is not None:
            parts.append(f"exit {code}")
        return " · ".join(parts)
    step = _text(section_title)
    if step and step.casefold() == _text(task_title).casefold():
        return ""
    return step


def _standing_attention_checks(
    task: Mapping[str, Any],
    canonical: Mapping[str, Any],
) -> tuple[Any, Any, list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """``(episode_of, state_of, failures, not_run)``: the Task's standing,
    not-superseded, not human-resolved failed checks and checks that could not
    run, with the lookups for each check's attention episode and state."""

    from .finding_disposition import finding_target_digest

    episodes = task.get("finding_episodes") if isinstance(task.get("finding_episodes"), list) else []
    episode_by_digest = {
        str(episode.get("target_digest")): episode
        for episode in episodes
        if isinstance(episode, Mapping) and episode.get("target_digest")
    }

    def episode_of(check: Mapping[str, Any]) -> Mapping[str, Any]:
        return episode_by_digest.get(str(finding_target_digest(check) or ""), {})

    def state_of(check: Mapping[str, Any]) -> str:
        return _text(episode_of(check).get("disposition_state")) or "open"

    def standing(results: frozenset[str]) -> list[Mapping[str, Any]]:
        return [
            check
            for check in canonical.get("latest_checks", [])
            if isinstance(check, Mapping)
            and _text(check.get("result")).lower() in results
            and _text(check.get("supersession_state")).lower() != "superseded"
            and state_of(check) != "resolved"
        ]

    return episode_of, state_of, standing(FAILED_CHECK_RESULTS), standing(NOT_RUN_CHECK_RESULTS)


def build_standing_attention(
    task: Mapping[str, Any],
    *,
    latest_store_activity_at: float | None = None,
    session_starts: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """What a reviewer will see for this Task, for an agent to read before it
    finishes: the verdict headline and EVERY standing attention item (not only
    the lead one), each as ``{kind, reason_label, label}``. Same reducers as the
    receipt, so the agent and the reviewer can never be told different things."""

    verification = step_verification_counts(task)
    checks = _project_checks(task)
    evidence_strength = _evidence_strength(task, checks, verification)
    canonical = reduce_task_outcome(
        task,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
    )
    decision = _decision_status(
        task,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
        canonical=canonical,
    )
    _episode, _state, failures, not_run = _standing_attention_checks(task, canonical)

    def _check_row(check: Mapping[str, Any], kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "reason_label": ATTENTION_REASON_LABELS[kind],
            "label": attention_label(
                kind,
                evidence_type=_text(check.get("evidence_type")) or None,
                result=_text(check.get("result")).lower() or None,
                check_name=check_display_name(check),
                exit_code=_int_or_none(check.get("exit_code")),
            ),
            "check_name": check_display_name(check),
        }

    items = [_check_row(check, "failed_check") for check in failures]
    key = _text(decision.get("key"))
    if key in {"failed", "blocked"}:
        kind = "failed_step" if key == "failed" else "blocker"
        title = _text(_mapping(decision.get("blocker")).get("step_title")) or None
        items.append(
            {
                "kind": kind,
                "reason_label": ATTENTION_REASON_LABELS[kind],
                "label": attention_label(kind, section_title=title),
                "check_name": None,
            }
        )
    items.extend(_check_row(check, "check_not_run") for check in not_run)
    return {
        "headline": verdict_headline(decision, evidence_strength),
        "decision_key": decision.get("key"),
        "items": items,
    }


def _attention_block(
    task: Mapping[str, Any],
    canonical: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    task_title: Any = None,
) -> tuple[int, dict[str, Any]] | None:
    """The ONE attention block: the lead item plus a count sentence
    (``more_text``) for every other open item behind it.

    Lead order: a standing failed check (a Finding), then a recorded failed
    step or blocker, then a check that could not run (a named gap, class 2 —
    never a Finding). Among checks, open items lead reviewed ones, and a
    human-resolved item never leads merely because it is newer.
    """

    from .finding_disposition import finding_target_digest

    _episode, _state, failures, not_run = _standing_attention_checks(task, canonical)
    key = _text(decision.get("key"))
    blocker = _mapping(decision.get("blocker"))
    blocker_count = int(blocker.get("blocked_step_count") or 0) if key in {"failed", "blocked"} else 0

    def _more(*, lead_kind: str) -> str | None:
        return more_attention_text(
            findings=len(failures) - (1 if lead_kind == "failed_check" else 0),
            blockers=max(0, blocker_count - (1 if lead_kind in {"blocker", "failed_step"} else 0)),
            not_run=len(not_run) - (1 if lead_kind == "check_not_run" else 0),
        )

    def _lead(candidates: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        preferred_state = "open" if any(_state(check) == "open" for check in candidates) else "reviewed"
        preferred = [check for check in candidates if _state(check) == preferred_state]
        return max(
            preferred or candidates,
            key=lambda check: (
                _check_observed_at(check),
                _int_or_none(check.get("arrival_sequence")) or 0,
            ),
        )

    def _check_item(check: Mapping[str, Any], kind: str) -> dict[str, Any]:
        observed_at = _check_observed_at(check)
        episode = _episode(check)
        state = _state(check)
        step = _linked_step(task, check)
        section_title = _text(step.get("title") or step.get("objective")) if step else ""
        check_name = check_display_name(check)
        evidence_type = _text(check.get("evidence_type")) or None
        result = _text(check.get("result")).lower() or None
        exit_code = _int_or_none(check.get("exit_code"))
        source = _check_source(check)
        latest = _mapping(episode.get("latest_disposition"))
        open_now = (
            bool(episode.get("attention_open"))
            if "attention_open" in episode
            else state == "open"
        )
        fallback_summary = (
            _DECISION_STATEMENTS["finding"]
            if kind == "failed_check"
            else f"A recorded check {CHECK_NOT_RUN_WORDS}; it proves nothing about the work."
        )
        return {
            "kind": kind,
            "reason_label": ATTENTION_REASON_LABELS[kind],
            "summary": _text(check.get("summary") or check.get("name") or check.get("evidence_type"))
            or fallback_summary,
            "check_name": check_name,
            "evidence_type": evidence_type,
            "result": result,
            "result_label": check_result_label(result),
            "result_tone": check_result_tone(result),
            "exit_code": exit_code,
            "section_title": section_title or None,
            "label": attention_label(
                kind,
                evidence_type=evidence_type,
                result=result,
                check_name=check_name,
                exit_code=exit_code,
            ),
            # A named result/exit-code disagreement (display only).
            "note_text": check_result_note(result, exit_code),
            # A check carries no inferred remedy: only the attached step's own
            # recorded next step, verbatim, when there is one.
            "next_step": (_text(step.get("next_step")) or None) if step else None,
            "observed_at": observed_at or None,
            "source": source,
            "source_label": source_label(source),
            "action_token": _text(episode.get("finding_token")) or None,
            "target_digest": str(finding_target_digest(check) or "") or None,
            "revision": int(episode.get("revision") or 0),
            "disposition_state": state,
            "disposition_note": _text(latest.get("note")) or None,
            "open": open_now,
            "effects": disposition_effects("finding" if kind == "failed_check" else "check_not_run"),
            "more_text": _more(lead_kind=kind),
        }

    # Preserve the dashboard's established mixed-state rule: a current failed
    # check leads even when the same Task also carries a recorded blocker.
    if failures:
        return (0, _check_item(_lead(failures), "failed_check"))

    if key in {"failed", "blocked"}:
        kind = "failed_step" if key == "failed" else "blocker"
        source = _asserted_by_source(_text(decision.get("asserted_by")), [])
        section_title = _text(blocker.get("step_title")) or None
        blocked_event_id = _text(blocker.get("blocked_event_id")) or None
        disposition = _mapping(blocker.get("disposition"))
        state = _text(disposition.get("state")) or "open"
        return (
            0 if key == "failed" else 1,
            {
                "kind": kind,
                "reason_label": ATTENTION_REASON_LABELS[kind],
                "summary": _text(
                    blocker.get("text") or blocker.get("step_title") or decision.get("statement")
                ),
                "check_name": None,
                "evidence_type": None,
                "result": None,
                "result_label": None,
                "result_tone": None,
                "exit_code": None,
                "section_title": section_title,
                "label": attention_label(kind, section_title=section_title, task_title=task_title),
                "note_text": None,
                "next_step": _text(blocker.get("next_step")) or None,
                "observed_at": _number(blocker.get("updated_at")) or None,
                "source": source,
                "source_label": source_label(source),
                # The write handle a blocker disposition names.
                "action_token": blocked_event_id,
                "target_digest": None,
                "revision": int(blocker.get("disposition_revision") or 0),
                "disposition_state": state,
                "disposition_note": _text(disposition.get("note")) or None,
                "open": state == "open",
                "effects": disposition_effects("blocked") if blocked_event_id else disposition_effects(None),
                "more_text": _more(lead_kind=kind),
            },
        )

    if not_run:
        return (2, _check_item(_lead(not_run), "check_not_run"))
    return None


def latest_store_activity(tasks: list[Mapping[str, Any]]) -> float | None:
    """The newest event time across every Task — the deterministic 'now' the
    outcome reducer compares against (never the wall clock). See
    ``reduce_task_outcome``'s ``latest_store_activity_at``."""

    newest = max(
        (task_newest_event_at(task) for task in tasks if isinstance(task, Mapping)),
        default=0.0,
    )
    return newest or None


def session_start_index(tasks: list[Mapping[str, Any]]) -> dict[str, float]:
    """Each session's START (earliest event timestamp) across the whole store.

    The sibling of ``latest_store_activity``: computed once by the caller that
    already holds every Task and threaded into ``reduce_task_outcome`` /
    ``build_receipt*`` as ``session_starts``. It maps ``client_session_id`` to the
    MINIMUM of that session's earliest event timestamp seen in any Task (a session
    may contribute work items / checks to more than one Task projection). Sessions'
    ``last_activity_at`` is a latest, not a start, so it is never used — see
    ``task_session_starts``. The went-quiet predicate uses this to require a
    genuinely NEWER session before it downgrades a Task."""

    starts: dict[str, float] = {}
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        for session_id, start in task_session_starts(task).items():
            if session_id not in starts or start < starts[session_id]:
                starts[session_id] = start
    return starts


__all__ = [
    "V1_ATTENTION_SCHEMA_VERSION",
    "CHECK_GROUPING_BY_REVISION",
    "CHECK_GROUPING_TIME_ORDER",
    "RECEIPT_SCHEMA_VERSION",
    "PROVENANCE_LEGEND",
    "EVIDENCE_TIER_LABEL",
    "ASSERTED_BY_LABELS",
    "ASSERTED_BY_PHRASES",
    "asserted_by_phrase",
    "evidence_ledger_parts",
    "evidence_unproven_parts",
    "not_gradeable_reason",
    "verdict_proof_clause",
    "COMMAND_NOT_SHOWN_TEXT",
    "asserted_by_label",
    "attention_label",
    "boundary_gap_text",
    "check_display_name",
    "check_recorded_summary",
    "check_tally_parts",
    "check_tally_text",
    "check_title",
    "cost_display_fields",
    "cost_gap_parts",
    "cost_state",
    "evidence_display_fields",
    "evidence_gap_parts",
    "revision_label",
    "source_entry",
    "verdict_gap",
    "build_receipt",
    "build_receipt_summary",
    "build_attention_reason",
    "build_standing_attention",
    "evidence_coverage_headline",
    "evidence_coverage_ledger",
    "receipt_cost_text",
    "receipt_category_text",
    "plan_share_headline",
    "verdict_headline",
    "verdict_gap_line",
    "verdict_health_window",
    "latest_store_activity",
    "session_start_index",
]
